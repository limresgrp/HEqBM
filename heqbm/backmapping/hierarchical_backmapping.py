import glob
import os
import time
import yaml
import torch

import numpy as np
import MDAnalysis as mda
import copy

from os.path import basename
# from e3nn import o3
from pathlib import Path
from typing import Dict, List, Optional
from MDAnalysis.analysis import align

from heqbm.mapper.hierarchical_mapper import HierarchicalMapper
# from heqbm.backmapping.nn.quaternions import get_quaternions, qv_mult
from heqbm.utils import DataDict
from heqbm.utils.geometry import get_RMSD, get_angles, get_dihedrals, set_phi, set_psi
from heqbm.utils.backbone import MinimizeEnergy
from heqbm.utils.pdbFixer import fixPDB
from heqbm.utils.minimisation import minimise_impl
from heqbm.utils.parsing import parse_slice

from heqbm.backmapping.allegro._keys import (
    INVARIANT_ATOM_FEATURES,
    EQUIVARIANT_ATOM_FEATURES,
    ATOM_POSITIONS,
)

from nequip.utils import Config
from nequip.utils._global_options import _set_global_options
from nequip.train import Trainer
from nequip.data import AtomicDataDict
from nequip.scripts.deploy import load_deployed_model, R_MAX_KEY


class HierarchicalBackmapping:

    config: Dict[str, str]
    mapping: HierarchicalMapper

    input_folder: Optional[str]
    input_filenames: List[str]
    model_config: Dict[str, str]
    model_r_max: float

    minimiser: MinimizeEnergy

    def __init__(self, args_dict) -> None:
        
        ### --------------------- ###
        self.config: Dict[str, str] = dict()

        config = args_dict.pop("config", None)
        if config is not None:
            self.config.update(yaml.safe_load(Path(config).read_text()))

        args_dict = {key: value for key, value in args_dict.items() if value is not None}
        self.config.update(args_dict)

        if self.config.get("trajslice", None) is not None:
            self.config["trajslice"] = parse_slice(self.config["trajslice"])

        ### Parse Input ###
        
        self.mapping = HierarchicalMapper(config=self.config)

        self.output_folder = self.config.get("output")
        input = self.config.get("input")
        if os.path.isdir(input):
            self.input_folder = input
            input_format = self.config.get("inputformat", "*")
            self.input_filenames = list(glob.glob(os.path.join(self.input_folder, f"*.{input_format}")))
        else:
            self.input_folder = None
            self.input_filename = input
            self.input_filenames = [self.input_filename]

        self.device = self.config.get("device", "cpu")

        ### Load Model ###
            
        print("Loading model...")

        model = self.config.get("model", None)
        if model is None:
            raise Exception("You did not provide the 'model' input parameter.")
        if Path(model).suffix not in [".yaml", ".json"]:
            try:
                deployed_model = os.path.join(os.path.dirname(__file__), '..', '..', deployed_model)
                self.model, metadata = load_deployed_model(
                    deployed_model,
                    device=self.config.get("device"),
                    set_global_options=True,  # don't warn that setting
                )
                # the global settings for a deployed model are set by
                # set_global_options in the call to load_deployed_model above
                self.model_r_max = float(metadata[R_MAX_KEY])
                print("Loaded deployed model")
            except Exception as e:
                raise Exception(
                    f"""Could not load {model}."""
                ) from e
        else:
            model_config_file = os.path.join(os.path.dirname(__file__), '..', '..', model)
            model_config: Optional[Dict[str, str]] = yaml.load(
                Path(model_config_file).read_text(), Loader=yaml.Loader
            ) if model_config_file is not None else None
            self.model, training_model_config = load_model(
                model_dir=None,
                model_config=model_config,
                config=self.config,
            )
            self.model_r_max = float(training_model_config[R_MAX_KEY])
            print("Loaded model from training session.")
        
        self.model.eval()

        ### Initialize energy minimiser for reconstructing backbone ###

        self.minimiser = MinimizeEnergy()

        ### ------------------------------------------------------- ###
    
    @property
    def num_structures(self):
        return len(self.input_filenames)
    
    def get_backmapping_dataset(self, frame_index: Optional[int] = None):
        if frame_index is None:
            return self.mapping.dataset
        backmapping_dataset = self.mapping.dataset
        for k in [
            DataDict.ATOM_POSITION,
            DataDict.ATOM_FORCES,
            DataDict.BEAD_POSITION,
            DataDict.BB_ATOM_POSITION,
            DataDict.BB_PHIPSI,
            DataDict.CA_ATOM_POSITION,
            DataDict.CA_BEAD_POSITION,
            DataDict.BEAD2ATOM_RELATIVE_VECTORS,
        ]:
            if k in backmapping_dataset:
                backmapping_dataset[k] = backmapping_dataset[k][frame_index:frame_index+1]
        return backmapping_dataset
    
    def optimise_backbone(
            self,
            backmapping_dataset: Dict,
            optimise_dihedrals: bool = False,
            verbose: bool = False,
    ):
        pred_u = build_universe(backmapping_dataset, 1, self.input_u.dimensions)
        positions_pred = backmapping_dataset[DataDict.ATOM_POSITION_PRED]
        not_nan_filter = ~np.any(np.isnan(positions_pred), axis=-1)
        pred_u.atoms.positions = positions_pred[not_nan_filter]
        
        minimiser_data = build_minimiser_data(dataset=backmapping_dataset)

        # Add predicted dihedrals as dihedral equilibrium values
        minimiser_data = update_minimiser_data(
            minimiser_data=minimiser_data,
            dataset=backmapping_dataset,
        )
        
        # Step 1: initial minimization: adjust bonds, angles and Omega dihedral
        self.minimiser.minimise(
            data=minimiser_data,
            dtau=self.config.get("bb_minimisation_dtau", 1e-1),
            eps=self.config.get("bb_initial_minimisation_eps", 1e-2),
            device=self.device,
            verbose=verbose,
        )

        if optimise_dihedrals:
            self.optimise_dihedrals(minimiser_data=minimiser_data)
        
        minimised_u = build_universe(backmapping_dataset, 1, self.input_u.dimensions)
        minimised_u.atoms.positions = minimiser_data["coords"][not_nan_filter]

        rmsds = align.alignto(
            minimised_u,  # mobile
            pred_u,       # reference
            select='name CA', # selection to operate on
            match_atoms=True
        ) # whether to match atoms

        backmapping_dataset[DataDict.ATOM_POSITION_PRED][not_nan_filter] = np.expand_dims(minimised_u.atoms.positions, axis=0)
        return backmapping_dataset
        
    def map(
        self,
        index: int,
        skip_if_existent: bool = True,
    ):
        if self.input_folder is None:
            self.mapping.map()
        else:
            self.input_filename = self.input_filenames[index]
            self.config["input"] = self.input_filename
            self.config["inputtraj"] = []
            self.config["output"] = os.path.join(self.output_folder, '.'.join(basename(self.input_filename).split('.')[:-1]))
            if skip_if_existent and os.path.isdir(self.config["output"]):
                return True
            print(f"Mapping structure {self.input_filename}")
            self.mapping.map()
        self.input_u = mda.Universe(self.config.get("input"))
        self.cg_u = build_CG(self.mapping.dataset)
        return False
    
    def backmap(
        self,
        frame_index: int,
        verbose: bool = False,
        optimise_backbone: Optional[bool] = None,
        optimise_dihedrals: bool = False,
    ):
        print(f"Backmapping structure {self.input_filename}")
        backmapping_dataset = self.get_backmapping_dataset(frame_index)

        print("Predicting distance vectors using HEqBM ENN & reconstructing atomistic structure...")
        t = time.time()

        # Predict dihedrals and bead2atom relative vectors
        backmapping_dataset = run_backmapping_inference(
            dataset=backmapping_dataset,
            model=self.model,
            r_max=self.model_r_max,
            device=self.device,
        )
        
        print(f"Finished. Time: {time.time() - t}")

        if optimise_backbone is None:
            optimise_backbone = self.config.get("optimizebackbone", True)

        if optimise_backbone:
            print("Optimizing backbone...")
            t = time.time()

            backmapping_dataset = self.optimise_backbone(
                backmapping_dataset=backmapping_dataset,
                optimise_dihedrals=optimise_dihedrals,
                verbose=verbose,
            )

            print(f"Finished. Time: {time.time() - t}")

        if DataDict.ATOM_POSITION in backmapping_dataset:
            try:
                print(f"RMSD All: {get_RMSD(backmapping_dataset[DataDict.ATOM_POSITION_PRED], backmapping_dataset[DataDict.ATOM_POSITION], ignore_nan=True):4.3f} Angstrom")
                no_bb_fltr = np.array([an not in ["CA", "C", "N", "O"] for an in backmapping_dataset[DataDict.ATOM_NAMES]])
                print(f"RMSD on Side-Chains: {get_RMSD(backmapping_dataset[DataDict.ATOM_POSITION_PRED], backmapping_dataset[DataDict.ATOM_POSITION], fltr=no_bb_fltr, ignore_nan=True):4.3f} Angstrom")
                bb_fltr = np.array([an in ["CA", "C", "N"] for an in backmapping_dataset[DataDict.ATOM_NAMES]])
                print(f"RMSD on Backbone: {get_RMSD(backmapping_dataset[DataDict.ATOM_POSITION_PRED], backmapping_dataset[DataDict.ATOM_POSITION], fltr=bb_fltr, ignore_nan=True):4.3f} Angstrom")
            except:
                pass
        
        return backmapping_dataset
    
    def optimise_dihedrals(self, minimiser_data: Dict):
        coords = minimiser_data["coords"][0]

        phi_idcs = minimiser_data["phi_idcs"]
        phi_values = minimiser_data["phi_values"]
        psi_idcs = minimiser_data["psi_idcs"]
        psi_values = minimiser_data["psi_values"]
        
        coords = set_phi(coords, phi_idcs, phi_values)
        coords = set_psi(coords, psi_idcs, psi_values)

    # def rotate_dihedrals_to_minimize_energy(self, minimiser_data: Dict, dataset: Dict):
    #     pred_pos = minimiser_data["coords"][0].detach().clone()
    #     ca_pos = pred_pos[np.isin(dataset[DataDict.ATOM_NAMES], ['CA'])]

    #     pi_quarters_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=np.pi/4)
    #     pi_halves_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=np.pi/2)
    #     pi_three_quarters_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=np.pi*3/4)
    #     pi_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=np.pi)
    #     minus_pi_quarters_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=-np.pi/4)
    #     minus_pi_halves_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=-np.pi/2)
    #     minus_pi_three_quarters_rotated_pred_pos = rotate_residue_dihedrals(pos=pred_pos, ca_pos=ca_pos, angle=-np.pi*3/4)
    #     all_rotated_pos = [
    #         pi_quarters_rotated_pred_pos,
    #         pi_halves_rotated_pred_pos,
    #         pi_three_quarters_rotated_pred_pos,
    #         pi_rotated_pred_pos,
    #         minus_pi_quarters_rotated_pred_pos,
    #         minus_pi_halves_rotated_pred_pos,
    #         minus_pi_three_quarters_rotated_pred_pos,
    #     ]

    #     # Rotate and evaluate one residue at a time
    #     updated_pos = pred_pos.clone()
    #     baseline_energy = self.minimiser.evaluate_dihedral_energy(minimiser_data, pos=pred_pos)
    #     for x in range(len(ca_pos)-1):
    #         temp_updated_pos = updated_pos.clone()
    #         C_id = x*4+2
    #         N_id = x*4+4
    #         energies = []
    #         for rotated_pos in all_rotated_pos:
    #             temp_updated_pos[C_id] = rotated_pos[C_id]
    #             temp_updated_pos[N_id] = rotated_pos[N_id]
    #             energies.append(self.minimiser.evaluate_dihedral_energy(minimiser_data, pos=temp_updated_pos))
    #         min_energy_id = torch.stack(energies).argmin()
    #         best_energy = energies[min_energy_id]
    #         if best_energy < baseline_energy:
    #             baseline_energy = best_energy
    #             updated_pos[C_id] = all_rotated_pos[min_energy_id][C_id]
    #             updated_pos[N_id] = all_rotated_pos[min_energy_id][N_id]

    #     minimiser_data["coords"] = updated_pos
    #     return minimiser_data
    
    def to_pdb(
            self,
            backmapping_dataset: Dict,
            n_frames: int,
            frame_index: int,
            backmapped_u: Optional[mda.Universe] = None,
            save_CG: bool = False,
        ):
        print(f"Saving structures...")
        t = time.time()
        output_folder = self.config["output"]
        os.makedirs(output_folder, exist_ok=True)

        # Write pdb file of CG structure
        if save_CG:
            cg_sel = self.cg_u.select_atoms('all')
            with mda.Writer(os.path.join(output_folder, f"CG_{frame_index}.pdb"), n_atoms=cg_sel.atoms.n_atoms) as w:
                w.write(cg_sel.atoms)
        
        if backmapped_u is None:
            backmapped_u = build_universe(backmapping_dataset, n_frames, self.input_u.dimensions)
        backmapped_u.trajectory[frame_index]
        positions_pred = backmapping_dataset[DataDict.ATOM_POSITION_PRED][0]
        
        # Write pdb of true atomistic structure (if present)
        if DataDict.ATOM_POSITION in backmapping_dataset:
            true_sel = backmapped_u.select_atoms('all')
            true_positions = backmapping_dataset[DataDict.ATOM_POSITION][0]
            true_sel.positions = true_positions[~np.any(np.isnan(positions_pred), axis=-1)]
            true_filename = os.path.join(output_folder, f"true_{frame_index}.pdb")
            with mda.Writer(true_filename, n_atoms=backmapped_u.atoms.n_atoms) as w:
                w.write(true_sel.atoms)

        # Write pdb of backmapped structure
        backmapped_sel = backmapped_u.select_atoms('all')
        backmapped_sel.positions = positions_pred[~np.any(np.isnan(positions_pred), axis=-1)]
        backmapped_filename = os.path.join(output_folder, f"backmapped_{frame_index}.pdb") 
        with mda.Writer(backmapped_filename, n_atoms=backmapped_u.atoms.n_atoms) as w:
            w.write(backmapped_sel.atoms)
        
        # Write pdb of minimised structure
        topology, positions = fixPDB(backmapped_filename, addHydrogens=True)
        backmapped_minimised_filename = os.path.join(output_folder, f"backmapped_min_{frame_index}.pdb")
        minimise_impl(
            topology,
            positions,
            backmapped_minimised_filename,
            restrain_atoms=['CA'],
            tolerance=200.,
        )

        print(f"Finished. Time: {time.time() - t}")

        return backmapped_u


def build_CG(
    backmapping_dataset: dict,
) -> mda.Universe:
    CG_u = mda.Universe.empty(
        n_atoms =       backmapping_dataset[DataDict.NUM_BEADS],
        n_residues =    backmapping_dataset[DataDict.NUM_RESIDUES],
        n_segments =    len(np.unique(backmapping_dataset[DataDict.BEAD_SEGIDS])),
        atom_resindex = backmapping_dataset[DataDict.BEAD_RESIDCS],
        trajectory =    True, # necessary for adding coordinates
    )
    CG_u.add_TopologyAttr('name',     backmapping_dataset[DataDict.BEAD_NAMES])
    CG_u.add_TopologyAttr('type',     backmapping_dataset[DataDict.BEAD_TYPES])
    CG_u.add_TopologyAttr('resname',  backmapping_dataset[DataDict.RESNAMES])
    CG_u.add_TopologyAttr('resid',    backmapping_dataset[DataDict.RESNUMBERS])
    CG_u.add_TopologyAttr('chainIDs', backmapping_dataset[DataDict.BEAD_SEGIDS])
    CG_u.load_new(np.nan_to_num(backmapping_dataset[DataDict.BEAD_POSITION]), order='fac')
    CG_u.dimensions = backmapping_dataset.get(DataDict.CELL, None)

    return CG_u

def build_universe(
    backmapping_dataset,
    n_frames,
    box_dimensions,
):
    nan_filter = ~np.any(np.isnan(backmapping_dataset[DataDict.ATOM_POSITION_PRED]), axis=-1)
    nan_filter = np.min(nan_filter, axis=0)
    num_atoms = nan_filter.sum()
    backmapped_u = mda.Universe.empty(
        n_atoms =       num_atoms,
        n_residues =    backmapping_dataset[DataDict.NUM_RESIDUES],
        n_segments =    len(np.unique(backmapping_dataset[DataDict.ATOM_SEGIDS])),
        atom_resindex = backmapping_dataset[DataDict.ATOM_RESIDCS][nan_filter],
        trajectory    = True # necessary for adding coordinates
    )
    coordinates = np.empty((
        n_frames,  # number of frames
        num_atoms,
        3,
    ))
    backmapped_u.load_new(coordinates, order='fac')

    backmapped_u.dimensions = box_dimensions
    backmapped_u.add_TopologyAttr('name',     backmapping_dataset[DataDict.ATOM_NAMES][nan_filter])
    backmapped_u.add_TopologyAttr('type',     backmapping_dataset[DataDict.ATOM_TYPES][nan_filter])
    backmapped_u.add_TopologyAttr('resname',  backmapping_dataset[DataDict.RESNAMES])
    backmapped_u.add_TopologyAttr('resid',    backmapping_dataset[DataDict.RESNUMBERS])
    backmapped_u.add_TopologyAttr('chainIDs', backmapping_dataset[DataDict.ATOM_SEGIDS][nan_filter])
    backmapped_u.dimensions = backmapping_dataset.get(DataDict.CELL, None)

    return backmapped_u

def load_model(config: Dict, model_dir: Optional[Path] = None, model_config: Optional[Dict] = None):
    if model_dir is None:
        assert model_config is not None, "You should provide either 'model_config_file' or 'model_dir' in the configuration file"
        model_dir = os.path.join(model_config.get("root"), model_config.get("fine_tuning_run_name", model_config.get("run_name")))
    model_name = config.get("modelweights", "best_model.pth")
    
    global_config = os.path.join(model_dir, "config.yaml")
    global_config = Config.from_file(str(global_config), defaults={})
    _set_global_options(global_config)
    del global_config

    model, training_model_config = Trainer.load_model_from_training_session(
        traindir=model_dir,
        model_name=model_name,
        device=config.get('device', 'cpu')
    )

    return model, training_model_config
    
def get_edge_index(positions: torch.Tensor, r_max: float):
    dist_matrix = torch.norm(positions[:, None, ...] - positions[None, ...], dim=-1).fill_diagonal_(torch.inf)
    return torch.argwhere(dist_matrix <= r_max).T.long()

def run_backmapping_inference(dataset: Dict, model: torch.nn.Module, r_max: float, device: str = 'cpu'):
    
    batch_max_atoms = 3000

    bead_pos = dataset[DataDict.BEAD_POSITION][0]
    bead_types = dataset[DataDict.BEAD_TYPES]
    bead_residcs = torch.from_numpy(dataset[DataDict.BEAD_RESIDCS]).long()
    bead_pos = torch.from_numpy(bead_pos).float()
    bead_types = torch.from_numpy(bead_types).long().reshape(-1, 1)
    edge_index = get_edge_index(positions=bead_pos, r_max=r_max)
    batch = torch.zeros(len(bead_pos), device=device, dtype=torch.long)
    bead2atom_idcs = torch.from_numpy(dataset[DataDict.BEAD2ATOM_RECONSTRUCTED_IDCS]).long()
    bead2atom_weights = torch.from_numpy(dataset[DataDict.BEAD2ATOM_RECONSTRUCTED_WEIGHTS]).float()
    lvl_idcs_mask = torch.from_numpy(dataset[DataDict.LEVEL_IDCS_MASK]).bool()
    lvl_idcs_anchor_mask = torch.from_numpy(dataset[DataDict.LEVEL_IDCS_ANCHOR_MASK]).long()
    data = {
        AtomicDataDict.POSITIONS_KEY: bead_pos,
        f"{AtomicDataDict.POSITIONS_KEY}_slices": torch.tensor([0, len(bead_pos)]),
        AtomicDataDict.ATOM_TYPE_KEY: bead_types,
        "edge_class": bead_residcs,
        AtomicDataDict.ORIG_EDGE_INDEX_KEY: edge_index,
        AtomicDataDict.EDGE_INDEX_KEY: edge_index,
        AtomicDataDict.BATCH_KEY: batch,
        DataDict.BEAD2ATOM_RECONSTRUCTED_IDCS: bead2atom_idcs,
        f"{DataDict.BEAD2ATOM_RECONSTRUCTED_IDCS}_slices": torch.tensor([0, len(bead2atom_idcs)]),
        DataDict.BEAD2ATOM_RECONSTRUCTED_WEIGHTS: bead2atom_weights,
        f"{DataDict.BEAD2ATOM_RECONSTRUCTED_WEIGHTS}_slices": torch.tensor([0, len(bead2atom_weights)]),
        DataDict.LEVEL_IDCS_MASK: lvl_idcs_mask,
        f"{DataDict.LEVEL_IDCS_MASK}_slices": torch.tensor([0, len(lvl_idcs_mask)]),
        DataDict.LEVEL_IDCS_ANCHOR_MASK: lvl_idcs_anchor_mask,
        f"{DataDict.ATOM_POSITION}_slices": torch.tensor([0, dataset[DataDict.ATOM_POSITION].shape[1]])
    }

    for v in data.values():
        v.to(device)

    already_computed_nodes = None
    chunk = already_computed_nodes is not None

    while True:
        batch_ = copy.deepcopy(data)
        batch_[AtomicDataDict.ORIG_BATCH_KEY] = batch_[AtomicDataDict.BATCH_KEY].clone()

        # Limit maximum batch size to avoid CUDA Out of Memory
        x = batch_[AtomicDataDict.EDGE_INDEX_KEY]
        x_ulen = len(x[0].unique())

        y = x.clone()
        
        if already_computed_nodes is not None:
            y = y[:, ~torch.isin(y[0], already_computed_nodes)]
        node_center_idcs = y[0].unique()
        if len(node_center_idcs) == 0:
            return

        offset = 0
        while len(y.unique()) > batch_max_atoms:
            chunk = True
            offset += 1
            
            def get_node_center_idcs(y: torch.Tensor, batch_max_atoms: int, offset: int = 1):
                unique_set = set()
                
                for i, num in enumerate(y[1]):
                    unique_set.add(num.item())

                    if len(unique_set) >= batch_max_atoms:
                        return torch.unique(y[0, :i+1])[:-offset]
                return torch.unique(y[0])

            def get_y_edge_filter(y: torch.Tensor, offset: int = 1):
                node_center_idcs = get_node_center_idcs(y, batch_max_atoms, offset)
                edge_filter = torch.isin(y[0], node_center_idcs)
                return edge_filter
            
            y_edge_filter = get_y_edge_filter(y, offset)
            y = y[:, y_edge_filter]
            
            #########################################################################################################
        

        if chunk:
            batch_[AtomicDataDict.EDGE_INDEX_KEY] = y
            x_edge_filter = torch.isin(x[0], y[0].unique())
            if AtomicDataDict.EDGE_CELL_SHIFT_KEY in batch_:
                batch_[AtomicDataDict.EDGE_CELL_SHIFT_KEY] = batch_[AtomicDataDict.EDGE_CELL_SHIFT_KEY][x_edge_filter]
            batch_[AtomicDataDict.BATCH_KEY] = batch_.get(AtomicDataDict.ORIG_BATCH_KEY, batch_[AtomicDataDict.BATCH_KEY])[y.unique()]
            
        del x
        
        # for slices_key, slices in data.__slices__.items():
        #     batch_[f"{slices_key}_slices"] = torch.tensor(slices, dtype=int)
        batch_["ptr"] = torch.nn.functional.pad(torch.bincount(batch_.get(AtomicDataDict.BATCH_KEY)).flip(dims=[0]), (0, 1), mode='constant').flip(dims=[0])
        
        # Remove all atoms that do not appear in edges and update edge indices
        edge_index = batch_[AtomicDataDict.EDGE_INDEX_KEY]

        edge_index_unique = edge_index.unique()

        ignore_chunk_keys = ["atom_pos"]
        
        for key in batch_.keys():
            if key in [
                AtomicDataDict.BATCH_KEY,
                AtomicDataDict.ORIG_BATCH_KEY,
                AtomicDataDict.EDGE_INDEX_KEY,
                AtomicDataDict.EDGE_CELL_SHIFT_KEY
            ] + ignore_chunk_keys:
                continue
            dim = np.argwhere(np.array(batch_[key].size()) == len(batch_[AtomicDataDict.ORIG_BATCH_KEY])).flatten()
            if len(dim) == 1:
                if dim[0] == 0:
                    batch_[key] = batch_[key][edge_index_unique]
                elif dim[0] == 1:
                    batch_[key] = batch_[key][:, edge_index_unique]
                elif dim[0] == 2:
                    batch_[key] = batch_[key][:, :, edge_index_unique]
                else:
                    raise Exception('Dimension not implemented')

        last_idx = -1
        batch_[AtomicDataDict.ORIG_EDGE_INDEX_KEY] = edge_index
        updated_edge_index = edge_index.clone()
        for idx in edge_index_unique:
            if idx > last_idx + 1:
                updated_edge_index[edge_index >= idx] -= idx - last_idx - 1
            last_idx = idx
        batch_[AtomicDataDict.EDGE_INDEX_KEY] = updated_edge_index

        node_index_unique = edge_index[0].unique()
        del edge_index
        del edge_index_unique

        for k, v in batch_.items():
            batch_[k] = v.to(device)

        with torch.no_grad():
            out = model(batch_)
            predicted_dih = out.get(INVARIANT_ATOM_FEATURES).cpu().numpy()
            predicted_b2a_rel_vec = out.get(EQUIVARIANT_ATOM_FEATURES).cpu().numpy()
            reconstructed_atom_pos = out.get(ATOM_POSITIONS, None)
            if reconstructed_atom_pos is not None:
                reconstructed_atom_pos = reconstructed_atom_pos.cpu().numpy()

        if already_computed_nodes is None:
            dataset[DataDict.BB_PHIPSI_PRED] = np.zeros((1, lvl_idcs_mask.shape[1], predicted_dih.shape[1]), dtype=float)
            dataset[DataDict.BEAD2ATOM_RELATIVE_VECTORS_PRED] = np.zeros((1, lvl_idcs_mask.shape[1], lvl_idcs_mask.shape[2], 3), dtype=float)
            if reconstructed_atom_pos is not None:
                dataset[DataDict.ATOM_POSITION_PRED] = reconstructed_atom_pos[None, ...]
        
        original_nodes = out[AtomicDataDict.ORIG_EDGE_INDEX_KEY][0].unique().cpu().numpy()
        nodes = out[AtomicDataDict.EDGE_INDEX_KEY][0].unique().cpu().numpy()
        
        dataset[DataDict.BB_PHIPSI_PRED][:, original_nodes] = predicted_dih[None, nodes]
        dataset[DataDict.BEAD2ATOM_RELATIVE_VECTORS_PRED][:, original_nodes] = predicted_b2a_rel_vec[None, nodes]
        if reconstructed_atom_pos is not None:
            fltr = np.argwhere(~np.isnan(reconstructed_atom_pos[:, 0])).flatten()
            dataset[DataDict.ATOM_POSITION_PRED][:, fltr] = reconstructed_atom_pos[None, fltr]
        
        del out

        if already_computed_nodes is None:
            if len(node_index_unique) < x_ulen:
                already_computed_nodes = node_index_unique
        elif len(already_computed_nodes) + len(node_index_unique) == x_ulen:
            already_computed_nodes = None
        else:
            assert len(already_computed_nodes) + len(node_index_unique) < x_ulen
            already_computed_nodes = torch.cat([already_computed_nodes, node_index_unique], dim=0)
        
        del batch_
        if already_computed_nodes is None:
            return dataset

def build_minimiser_data(dataset: Dict):

    atom_names = dataset[DataDict.ATOM_NAMES]
    dataset_bond_idcs = dataset[DataDict.BOND_IDCS]
    dataset_angle_idcs = dataset[DataDict.ANGLE_IDCS]
    
    bond_idcs = []
    bond_eq_val = []
    bond_tolerance = []

    angle_idcs = []
    angle_eq_val = []
    angle_tolerance = []

    N_CA_filter = np.all(atom_names[dataset_bond_idcs] == ['N', 'CA'], axis=1)
    bond_idcs.append(dataset_bond_idcs[N_CA_filter])
    bond_eq_val.append([1.45] * N_CA_filter.sum())
    bond_tolerance.append([0.02] * N_CA_filter.sum())
    CA_C_filter = np.all(atom_names[dataset_bond_idcs] == ['CA', 'C'], axis=1)
    bond_idcs.append(dataset_bond_idcs[CA_C_filter])
    bond_eq_val.append([1.52] * CA_C_filter.sum())
    bond_tolerance.append([0.02] * CA_C_filter.sum())
    C_O_filter = np.all(atom_names[dataset_bond_idcs] == ['C', 'O'], axis=1)
    bond_idcs.append(dataset_bond_idcs[C_O_filter])
    bond_eq_val.append([1.24] * C_O_filter.sum())
    bond_tolerance.append([0.02] * C_O_filter.sum())
    C_N_filter = np.all(atom_names[dataset_bond_idcs] == ['C', 'N'], axis=1)
    bond_idcs.append(dataset_bond_idcs[C_N_filter])
    bond_eq_val.append([1.32] * C_N_filter.sum())
    bond_tolerance.append([0.02] * C_N_filter.sum())

    N_CA_C_filter = np.all(atom_names[dataset_angle_idcs] == ['N', 'CA', 'C'], axis=1)
    angle_idcs.append(dataset_angle_idcs[N_CA_C_filter])
    angle_eq_val.append([1.9216075] * N_CA_C_filter.sum()) # 110.1 degrees
    angle_tolerance.append([0.035] * N_CA_C_filter.sum())
    CA_C_O_filter = np.all(atom_names[dataset_angle_idcs] == ['CA', 'C', 'O'], axis=1)
    angle_idcs.append(dataset_angle_idcs[CA_C_O_filter])
    angle_eq_val.append([2.1031217] * CA_C_O_filter.sum()) # 120.5 degrees
    angle_tolerance.append([0.035] * CA_C_O_filter.sum())
    CA_C_N_filter = np.all(atom_names[dataset_angle_idcs] == ['CA', 'C', 'N'], axis=1)
    angle_idcs.append(dataset_angle_idcs[CA_C_N_filter])
    angle_eq_val.append([2.0350539] * CA_C_N_filter.sum()) # 116.6 degrees
    angle_tolerance.append([0.035] * CA_C_N_filter.sum())
    O_C_N_filter = np.all(atom_names[dataset_angle_idcs] == ['O', 'C', 'N'], axis=1)
    angle_idcs.append(dataset_angle_idcs[O_C_N_filter])
    angle_eq_val.append([2.14675] * O_C_N_filter.sum()) # 123.0 degrees
    angle_tolerance.append([0.035] * O_C_N_filter.sum())
    C_N_CA_filter = np.all(atom_names[dataset_angle_idcs] == ['C', 'N', 'CA'], axis=1)
    angle_idcs.append(dataset_angle_idcs[C_N_CA_filter])
    angle_eq_val.append([2.1275564] * C_N_CA_filter.sum()) # 121.9 degrees
    angle_tolerance.append([0.035] * C_N_CA_filter.sum())
    
    # ------------------------------------------------------------------------------- #

    bond_idcs = np.concatenate(bond_idcs)
    bond_eq_val = np.concatenate(bond_eq_val)
    bond_tolerance = np.concatenate(bond_tolerance)

    angle_idcs = np.concatenate(angle_idcs)
    angle_eq_val = np.concatenate(angle_eq_val)
    angle_tolerance = np.concatenate(angle_tolerance)

    data = {
        "bond_idcs": bond_idcs,
        "bond_eq_val": bond_eq_val,
        "bond_tolerance": bond_tolerance,
        "angle_idcs": angle_idcs,
        "angle_eq_val": angle_eq_val,
        "angle_tolerance": angle_tolerance,
    }
    
    return data

def update_minimiser_data(minimiser_data: Dict, dataset: Dict):
    atom_names = dataset[DataDict.ATOM_NAMES]
    dataset_torsion_idcs = dataset[DataDict.TORSION_IDCS]

    omega_idcs = dataset_torsion_idcs[np.all(atom_names[dataset_torsion_idcs] == np.array(['CA', 'C', 'N', 'CA']), axis=-1)]
    omega_values = np.array([np.pi] * len(omega_idcs))
    omega_tolerance = np.array([0.436332] * len(omega_idcs)) # 25 deg

    bead_names = dataset[DataDict.BEAD_NAMES]
    bb_bead_idcs = np.isin(bead_names, ['BB'])
    bb_bead_coords = dataset[DataDict.BEAD_POSITION][:, bb_bead_idcs]
    bb_atom_idcs: np.ma.masked_array = dataset[DataDict.BEAD2ATOM_RECONSTRUCTED_IDCS][bb_bead_idcs]
    bb_atom_names = atom_names[bb_atom_idcs]
    bb_atom_names[bb_atom_idcs.mask] = ''
    bb_atom_idcs[~np.isin(bb_atom_names, ['CA', 'C', 'N', 'O'])] = -1
    bb_atom_weights: np.ma.masked_array = dataset[DataDict.BEAD2ATOM_RECONSTRUCTED_WEIGHTS][bb_bead_idcs]

    data = {
        "coords":          dataset[DataDict.ATOM_POSITION_PRED],
        "bb_bead_coords":  bb_bead_coords,
        "bb_atom_idcs":    bb_atom_idcs,
        "bb_atom_weights": bb_atom_weights,
        "atom_names":      atom_names,
        "omega_idcs":      omega_idcs,
        "omega_values":    omega_values,
        "omega_tolerance": omega_tolerance,
    }

    if DataDict.BB_PHIPSI_PRED in dataset and dataset[DataDict.BB_PHIPSI_PRED].shape[-1] == 2:
        pred_torsion_values = dataset[DataDict.BB_PHIPSI_PRED][0, bb_bead_idcs]

        phi_torsion_idcs = dataset_torsion_idcs[np.all(atom_names[dataset_torsion_idcs] == np.array(['C', 'N', 'CA', 'C']), axis=-1)]
        psi_torsion_idcs = dataset_torsion_idcs[np.all(atom_names[dataset_torsion_idcs] == np.array(['N', 'CA', 'C', 'N']), axis=-1)]
        
        phi_idcs = []
        phi_values = []
        psi_idcs = []
        psi_values = []

        for i, (phi_value, psi_value) in enumerate(pred_torsion_values):
            if i > 0:
                phi_idcs.append(phi_torsion_idcs[i - 1])
                phi_values.append(phi_value)
            if i < len(pred_torsion_values) - 1:
                psi_idcs.append(psi_torsion_idcs[i])
                psi_values.append(psi_value)

        phi_idcs = np.stack(phi_idcs, axis=0)
        phi_values = np.stack(phi_values, axis=0)
        psi_idcs = np.stack(psi_idcs, axis=0)
        psi_values = np.stack(psi_values, axis=0)

        data.update({
            "phi_idcs": phi_idcs,
            "phi_values": phi_values,
            "psi_idcs": psi_idcs,
            "psi_values": psi_values,
        })
    
    minimiser_data.update(data)

    return minimiser_data

# def rotate_residue_dihedrals(pos: torch.TensorType, ca_pos: torch.TensorType, angle: float):
#     rot_axes = ca_pos[1:] - ca_pos[:-1]
#     rot_axes = rot_axes / torch.norm(rot_axes, dim=-1, keepdim=True)
#     rot_axes = rot_axes.repeat_interleave(2 * torch.ones((len(rot_axes),), dtype=int, device=rot_axes.device), dim=0)

#     angles_polar = 0.5 * angle * torch.ones((len(rot_axes),), dtype=float, device=rot_axes.device).reshape(-1, 1)

#     q_polar = get_quaternions(
#         batch=1,
#         rot_axes=rot_axes,
#         angles=angles_polar
#     )

#     C_N_O_fltr = torch.zeros((len(pos),), dtype=bool, device=rot_axes.device)
#     for x in range(len(ca_pos)-1):
#         C_N_O_fltr[x*4+2] = True
#         C_N_O_fltr[x*4+4] = True

#     v_ = pos[C_N_O_fltr] - ca_pos[:-1].repeat_interleave(2 * torch.ones((len(ca_pos[:-1]),), dtype=int, device=ca_pos.device), dim=0)
#     v_rotated = qv_mult(q_polar, v_)

#     rotated_pred_pos = pos.clone()
#     rotated_pred_pos[C_N_O_fltr] = ca_pos[:-1].repeat_interleave(2 * torch.ones((len(ca_pos[:-1]),), dtype=int, device=ca_pos.device), dim=0) + v_rotated
#     return rotated_pred_pos

# def adjust_bb_oxygens(dataset: Dict, position_key: str = DataDict.ATOM_POSITION_PRED):
#     atom_CA_idcs = dataset[DataDict.CA_ATOM_IDCS]
#     no_cappings_filter = [x.split('_')[0] not in ['ACE', 'NME'] for x in dataset[DataDict.ATOM_NAMES]]
#     atom_C_idcs = np.array([ncf and an.split('_')[1] in ["C"] for ncf, an in zip(no_cappings_filter, dataset[DataDict.ATOM_NAMES])])
#     atom_O_idcs = np.array([ncf and an.split('_')[1] in ["O"] for ncf, an in zip(no_cappings_filter, dataset[DataDict.ATOM_NAMES])])

#     ca_pos = dataset[position_key][:, atom_CA_idcs]

#     c_o_vectors = []
#     for i in range(ca_pos.shape[1]-2):
#         ca_i, ca_ii, ca_iii = ca_pos[:, i], ca_pos[:, i+1], ca_pos[:, i+2]
#         ca_i_ca_ii = ca_ii - ca_i
#         ca_i_ca_iii = ca_iii - ca_i
#         c_o = np.cross(ca_i_ca_ii, ca_i_ca_iii, axis=1)
#         c_o = c_o / np.linalg.norm(c_o, axis=-1, keepdims=True) * 1.229 # C-O bond legth
#         c_o_vectors.append(c_o)
#     # Last missing vectors
#     for _ in range(len(c_o_vectors), atom_C_idcs.sum()):
#         c_o_vectors.append(c_o)
#     c_o_vectors = np.array(c_o_vectors).swapaxes(0,1)

#     o_pos = dataset[position_key][:, atom_C_idcs] + c_o_vectors
#     dataset[position_key][:, atom_O_idcs] = o_pos

#     pos = torch.from_numpy(dataset[position_key]).float()
#     omega_dihedral_idcs = torch.from_numpy(dataset[DataDict.OMEGA_DIH_IDCS]).long()
#     adjusted_pos = adjust_oxygens(
#         pos=pos,
#         omega_dihedral_idcs=omega_dihedral_idcs,
#     )
#     dataset[position_key] = adjusted_pos.cpu().numpy()

#     return dataset

# def adjust_oxygens(
#         pos: torch.Tensor, # (batch, n_atoms, 3)
#         omega_dihedral_idcs: torch.Tensor, # (n_dih, 4)
#     ):
#     batch = pos.size(0)

#     # ADJUST DIHEDRAL ANGLE [O, C, N, CA]
#     dih_values = get_dihedrals(pos, omega_dihedral_idcs) # (batch, n_dih)

#     # omega_dihedral_idcs represent atoms [O, C, N, CA]
#     v_ = pos[:, omega_dihedral_idcs[:, 0]] - pos[:, omega_dihedral_idcs[:, 1]]
#     v_ = v_.reshape(-1, 3) # (batch * n_peptide_oxygens, xyz)

#     rot_axes = pos[:, omega_dihedral_idcs[:, 2]] - pos[:, omega_dihedral_idcs[:, 1]]
#     rot_axes = rot_axes / rot_axes.norm(dim=-1, keepdim=True)
#     rot_axes = rot_axes.reshape(-1, 3) # (batch * n_peptide_oxygens, xyz)

#     # We want that the final dihedral values are 0, so we rotate of dih_values
#     angles_polar = dih_values.reshape(-1, 1) # (n_peptide_oxygens, 1)
#     q_polar = get_quaternions(               # (batch * n_peptide_oxygens, 4)
#         batch=batch,
#         rot_axes=rot_axes,
#         angles=0.5*angles_polar
#     )

#     v_rotated = qv_mult(q_polar, v_).reshape(batch, -1, 3) # (batch, n_peptide_oxygens, xyz)
#     adjusted_pos = pos.clone()
#     adjusted_pos[:, omega_dihedral_idcs[:, 0]] = adjusted_pos[:, omega_dihedral_idcs[:, 1]] + v_rotated

#     # ADJUST ANGLE [O, C, N]
#     angle_values, b0, b1 = get_angles(adjusted_pos, omega_dihedral_idcs[:, :3], return_vectors=True)
#     # angle_values (batch, n_angles)
#     # b0 (batch, n_angles, xyz) C->O
#     # b1 (batch, n_angles, xyz) C->N

#     v_ = adjusted_pos[:, omega_dihedral_idcs[:, 0]] - adjusted_pos[:, omega_dihedral_idcs[:, 1]]
#     v_ = v_.reshape(-1, 3) # (batch * n_peptide_oxygens, xyz)

#     cross_prod = o3.TensorProduct(
#         "1x1o",
#         "1x1o",
#         "1x1e",
#         [(0, 0, 0, "uuu", False)],
#         irrep_normalization='none',
#     )
#     rot_axes = cross_prod(b0, b1) # (batch, n_peptide_oxygens, xyz)
#     rot_axes = rot_axes / rot_axes.norm(dim=-1, keepdim=True)
#     rot_axes = rot_axes.reshape(-1, 3) # (batch * n_peptide_oxygens, xyz)

#     angles_polar = angle_values.reshape(-1, 1) - 2.145 # (n_peptide_oxygens, 1) | O-C-N angle eq value is 122.9 degrees
#     angles_polar[angles_polar > np.pi] -= 2*np.pi
#     angles_polar[angles_polar < -np.pi] += 2*np.pi
#     q_polar = get_quaternions(               # (batch * n_peptide_oxygens, 4)
#         batch=batch,
#         rot_axes=rot_axes,
#         angles=0.5*angles_polar
#     )

#     v_rotated = qv_mult(q_polar, v_).reshape(batch, -1, 3) # (batch, n_peptide_oxygens, xyz)
#     adjusted_pos[:, omega_dihedral_idcs[:, 0]] = adjusted_pos[:, omega_dihedral_idcs[:, 1]] + v_rotated

#     return adjusted_pos