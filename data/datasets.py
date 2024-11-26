import abc
import numpy as np
import pandas as pd
import logging
import tree
import torch
import random

from torch.utils.data import Dataset
from data import utils as du
from openfold.data import data_transforms
from openfold.utils import rigid_utils
from sklearn.preprocessing import PolynomialFeatures
from sklearn.linear_model import LinearRegression


def _rog_filter(df, quantile):
    y_quant = pd.pivot_table(
        df,
        values='radius_gyration', 
        index='modeled_seq_len',
        aggfunc=lambda x: np.quantile(x, quantile)
    )
    x_quant = y_quant.index.to_numpy()
    y_quant = y_quant.radius_gyration.to_numpy()

    # Fit polynomial regressor
    poly = PolynomialFeatures(degree=4, include_bias=True)
    poly_features = poly.fit_transform(x_quant[:, None])
    poly_reg_model = LinearRegression()
    poly_reg_model.fit(poly_features, y_quant)

    # Calculate cutoff for all sequence lengths
    max_len = df.modeled_seq_len.max()
    pred_poly_features = poly.fit_transform(np.arange(max_len)[:, None])
    # Add a little more.
    pred_y = poly_reg_model.predict(pred_poly_features) + 0.1

    row_rog_cutoffs = df.modeled_seq_len.map(lambda x: pred_y[x-1])
    return df[df.radius_gyration < row_rog_cutoffs]


def _length_filter(data_csv, min_res, max_res):
    return data_csv[
        (data_csv.modeled_seq_len >= min_res)
        & (data_csv.modeled_seq_len <= max_res)
    ]


def _plddt_percent_filter(data_csv, min_plddt_percent):
    return data_csv[data_csv.num_confident_plddt > min_plddt_percent]


def _max_coil_filter(data_csv, max_coil_percent):
    return data_csv[data_csv.coil_percent <= max_coil_percent]


def _process_csv_row(processed_file_path):
    processed_feats = du.read_pkl(processed_file_path)
    processed_feats = du.parse_chain_feats(processed_feats)

    # Only take modeled residues.
    modeled_idx = processed_feats['modeled_idx']
    min_idx = np.min(modeled_idx)
    max_idx = np.max(modeled_idx)
    del processed_feats['modeled_idx']
    processed_feats = tree.map_structure(
        lambda x: x[min_idx:(max_idx+1)], processed_feats)

    # Run through OpenFold data transforms.
    chain_feats = {
        'aatype': torch.tensor(processed_feats['aatype']).long(),
        'all_atom_positions': torch.tensor(processed_feats['atom_positions']).double(),
        'all_atom_mask': torch.tensor(processed_feats['atom_mask']).double()
    }
    chain_feats = data_transforms.atom37_to_frames(chain_feats)
    rigids_1 = rigid_utils.Rigid.from_tensor_4x4(chain_feats['rigidgroups_gt_frames'])[:, 0]
    rotmats_1 = rigids_1.get_rots().get_rot_mats()
    trans_1 = rigids_1.get_trans()
    res_plddt = processed_feats['b_factors'][:, 1]
    res_mask = torch.tensor(processed_feats['bb_mask']).int()

    # Re-number residue indices for each chain such that it starts from 1.
    # Randomize chain indices.
    chain_idx = processed_feats['chain_index']
    res_idx = processed_feats['residue_index']
    new_res_idx = np.zeros_like(res_idx)
    new_chain_idx = np.zeros_like(res_idx)
    all_chain_idx = np.unique(chain_idx).tolist()
    shuffled_chain_idx = np.array(
        random.sample(all_chain_idx, len(all_chain_idx))) - np.min(all_chain_idx) + 1
    for i,chain_id in enumerate(all_chain_idx):
        chain_mask = (chain_idx == chain_id).astype(int)
        chain_min_idx = np.min(res_idx + (1 - chain_mask) * 1e3).astype(int)
        new_res_idx = new_res_idx + (res_idx - chain_min_idx + 1) * chain_mask

        # Shuffle chain_index
        replacement_chain_id = shuffled_chain_idx[i]
        new_chain_idx = new_chain_idx + replacement_chain_id * chain_mask
    if torch.isnan(trans_1).any() or torch.isnan(rotmats_1).any():
        raise ValueError(f'Found NaNs in {processed_file_path}')
    return {
        'aatype': chain_feats['aatype'],
        'rotmats_1': rotmats_1,
        'trans_1': trans_1,
        'res_mask': res_mask,
        'res_idx': new_res_idx,
    }


def _add_plddt_mask(feats, plddt_threshold):
    feats['plddt_mask'] = torch.tensor(
        feats['res_plddt'] > plddt_threshold).int()


def _read_clusters(cluster_path):
    pdb_to_cluster = {}
    with open(cluster_path, "r") as f:
        for i,line in enumerate(f):
            for chain in line.split(' '):
                pdb = chain.split('_')[0]
                pdb_to_cluster[pdb.upper()] = i
    return pdb_to_cluster


class BaseDataset(Dataset):
    def __init__(
            self,
            *,
            dataset_cfg,
            is_training,
        ):
        self._log = logging.getLogger(__name__)
        self._is_training = is_training
        self._dataset_cfg = dataset_cfg
        self.raw_csv = pd.read_csv(self.dataset_cfg.csv_path)
        metadata_csv = self._filter_metadata(self.raw_csv)
        metadata_csv = metadata_csv.sort_values(
            'modeled_seq_len', ascending=False)
        self._create_split(metadata_csv)
        self._cache = {}
        self._rng = np.random.default_rng(seed=self._dataset_cfg.seed)

    @property
    def is_training(self):
        return self._is_training

    @property
    def dataset_cfg(self):
        return self._dataset_cfg
    
    def __len__(self):
        return len(self.csv)

    @abc.abstractmethod
    def _filter_metadata(self, raw_csv: pd.DataFrame) -> pd.DataFrame:
        pass

    def _create_split(self, data_csv):
        # Training or validation specific logic.
        if self.is_training:
            self.csv = data_csv
            self._log.info(
                f'Training: {len(self.csv)} examples')
        else:
            if self._dataset_cfg.max_eval_length is None:
                eval_lengths = data_csv.modeled_seq_len
            else:
                eval_lengths = data_csv.modeled_seq_len[
                    data_csv.modeled_seq_len <= self._dataset_cfg.max_eval_length
                ]
            all_lengths = np.sort(eval_lengths.unique())
            length_indices = (len(all_lengths) - 1) * np.linspace(
                0.0, 1.0, self.dataset_cfg.num_eval_lengths)
            length_indices = length_indices.astype(int)
            eval_lengths = all_lengths[length_indices]
            eval_csv = data_csv[data_csv.modeled_seq_len.isin(eval_lengths)]

            # Fix a random seed to get the same split each time.
            eval_csv = eval_csv.groupby('modeled_seq_len').sample(
                self.dataset_cfg.samples_per_eval_length,
                replace=True,
                random_state=123
            )
            eval_csv = eval_csv.sort_values('modeled_seq_len', ascending=False)
            self.csv = eval_csv
            self._log.info(
                f'Validation: {len(self.csv)} examples with lengths {eval_lengths}')
        self.csv['index'] = list(range(len(self.csv)))

    def process_csv_row(self, csv_row):
        path = csv_row['processed_path']
        seq_len = csv_row['modeled_seq_len']
        # Large protein files are slow to read. Cache them.
        use_cache = seq_len > self._dataset_cfg.cache_num_res
        if use_cache and path in self._cache:
            return self._cache[path]
        processed_row = _process_csv_row(path)
        if use_cache:
            self._cache[path] = processed_row
        return processed_row

    def __getitem__(self, row_idx):
        # Process data example.
        csv_row = self.csv.iloc[row_idx]
        feats = self.process_csv_row(csv_row)
        feats['csv_idx'] = torch.ones(1, dtype=torch.long) * row_idx
        return feats


class ScopeDataset(BaseDataset):

    def _filter_metadata(self, raw_csv):
        filter_cfg = self.dataset_cfg.filter
        data_csv = _length_filter(
            raw_csv,
            filter_cfg.min_num_res,
            filter_cfg.max_num_res
        )
        data_csv['oligomeric_detail'] = 'monomeric'
        return data_csv


class PdbDataset(BaseDataset):

    def __init__(
            self,
            *,
            dataset_cfg,
            is_training,
        ):
        self._log = logging.getLogger(__name__)
        self._is_training = is_training
        self._dataset_cfg = dataset_cfg
        self._cache = {}
        self._rng = np.random.default_rng(seed=self._dataset_cfg.seed)

        # Process clusters
        self.raw_csv = pd.read_csv(self.dataset_cfg.csv_path)
        metadata_csv = self._filter_metadata(self.raw_csv)
        metadata_csv = metadata_csv.sort_values(
            'modeled_seq_len', ascending=False)

        self._pdb_to_cluster = _read_clusters(self._dataset_cfg.cluster_path)
        self._max_cluster = max(self._pdb_to_cluster.values())
        self._missing_pdbs = 0
        def cluster_lookup(pdb):
            pdb = pdb.upper()
            if pdb not in self._pdb_to_cluster:
                self._pdb_to_cluster[pdb] = self._max_cluster + 1
                self._max_cluster += 1
                self._missing_pdbs += 1
            return self._pdb_to_cluster[pdb]
        metadata_csv['cluster'] = metadata_csv['pdb_name'].map(cluster_lookup)
        self._create_split(metadata_csv)
        self._all_clusters = dict(
            enumerate(self.csv['cluster'].unique().tolist()))
        self._num_clusters = len(self._all_clusters)

    def _filter_metadata(self, raw_csv):
        """Filter metadata."""
        filter_cfg = self.dataset_cfg.filter
        data_csv = raw_csv[
            raw_csv.oligomeric_detail.isin(filter_cfg.oligomeric)]
        data_csv = data_csv[
            data_csv.num_chains.isin(filter_cfg.num_chains)]
        data_csv = _length_filter(
            data_csv, filter_cfg.min_num_res, filter_cfg.max_num_res)
        data_csv = _max_coil_filter(data_csv, filter_cfg.max_coil_percent)
        data_csv = _rog_filter(data_csv, filter_cfg.rog_quantile)
        return data_csv
