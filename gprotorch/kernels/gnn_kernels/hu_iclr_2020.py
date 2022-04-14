"""
Reformatted and cleaned up GNN modules from Hu et al.
Strategies for Pre-training Graph Neural Networks. ICLR 2020
(https://github.com/snap-stanford/pretrain-gnns)

Author: Leo Klarner (https://github.com/leojklarner), April 2022
"""

import torch
import numpy as np
from torch_geometric.nn import MessagePassing
from torch_geometric.utils import add_self_loops
from torch_geometric.data import Data
import torch.nn.functional as F
from torch_scatter import scatter_add
from rdkit import Chem

# define the allowable node and edge labels as used in Hu et al.
allowable_features = {
    'possible_atomic_num_list': list(range(1, 119)),
    'possible_chirality_list': [
        Chem.rdchem.ChiralType.CHI_UNSPECIFIED,
        Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW,
        Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CCW,
        Chem.rdchem.ChiralType.CHI_OTHER
    ],
    'possible_bonds': [
        Chem.rdchem.BondType.SINGLE,
        Chem.rdchem.BondType.DOUBLE,
        Chem.rdchem.BondType.TRIPLE,
        Chem.rdchem.BondType.AROMATIC
    ],
    # (E)/(Z) double bond stereo information
    'possible_bond_dirs': [
        Chem.rdchem.BondDir.NONE,
        Chem.rdchem.BondDir.ENDUPRIGHT,
        Chem.rdchem.BondDir.ENDDOWNRIGHT
    ]
}

# define additional tokens for label masking
num_atom_type = 120  # including the extra mask tokens
num_chirality_tag = 3

num_bond_type = 6  # including aromatic and self-loop edge, and extra masked tokens
num_bond_direction = 3
self_loop_token = 4  # bond type for self-loop edge
masked_bond_token = 5  # bond type for masked edges


def mol_to_pyg(mol):
    """
    A featuriser that accepts an rdkit mol instance and
    converts it to a PyTorch Geometric data object that
    is compatible with the GNN modules below.

    Args:
        mol: rdkit mol object

    Returns: PyTorch Geometric data object

    """

    # derive atom features: atomic number + chirality tag
    atom_features = []
    for atom in mol.GetAtoms():
        atom_features.append([
            allowable_features['possible_atomic_num_list'].index(atom.GetAtomicNum()),
            allowable_features['possible_chirality_list'].index(atom.GetChiralTag())
        ])
    atom_features = torch.tensor(np.array(atom_features), dtype=torch.long)

    # derive bond features: bond type + bond direction
    # PyTorch Geometric only uses directed edges,
    # so feature information needs to be added twice
    edge_index = []
    edge_attr = []
    for bond in mol.GetBonds():
        i = bond.GetBeginAtomIdx()
        j = bond.GetEndAtomIdx()
        edge_index.append((i, j))
        edge_index.append((j, i))

        # calculate edge features and append them to feature list
        edge_feature = [allowable_features['possible_bonds'].index(bond.GetBondType()),
                        allowable_features['possible_bond_dirs'].index(bond.GetBondDir())]
        edge_attr.append(edge_feature)
        edge_attr.append(edge_feature)

    # set data.edge_index: Graph connectivity in COO format with shape [2, num_edges]
    edge_index = torch.tensor(np.array(edge_index).T, dtype=torch.long)

    # set data.edge_attr: Edge feature matrix with shape [num_edges, num_edge_features]
    edge_attr = torch.tensor(np.array(edge_attr), dtype=torch.long)

    return Data(x=atom_features, edge_index=edge_index, edge_attr=edge_attr)


class GINConv(MessagePassing):
    """
    Extension of the Graph Isomorphism Network to incorporate
    edge information by concatenating edge embeddings.
    """

    def __init__(self, emb_dim, aggr="add"):
        super(GINConv, self).__init__()

        self.mlp = torch.nn.Sequential(
            torch.nn.Linear(emb_dim, 2 * emb_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(2 * emb_dim, emb_dim)
        )

        self.edge_embedding1 = torch.nn.Embedding(num_bond_type, emb_dim)
        self.edge_embedding2 = torch.nn.Embedding(num_bond_direction, emb_dim)
        torch.nn.init.xavier_uniform_(self.edge_embedding1.weight.data)
        torch.nn.init.xavier_uniform_(self.edge_embedding2.weight.data)

        self.aggr = aggr

    def forward(self, x, edge_index, edge_attr):

        # add self loops to edge index
        edge_index, _ = add_self_loops(edge_index, num_nodes=x.size(0))

        # update edge attributes to represent self-loop edges
        self_loop_attr = torch.zeros(x.size(0), 2)
        self_loop_attr[:, 0] = self_loop_token
        self_loop_attr = self_loop_attr.to(edge_attr.device).to(edge_attr.dtype)
        edge_attr = torch.cat((edge_attr, self_loop_attr), dim=0)

        # generate edge embeddings and propagate
        edge_embeddings = self.edge_embedding1(edge_attr[:, 0]) + self.edge_embedding2(edge_attr[:, 1])
        return self.propagate(edge_index, x=x, edge_attr=edge_embeddings)

    def message(self, x_j, edge_attr):
        return x_j + edge_attr

    def update(self, aggr_out):
        return self.mlp(aggr_out)


class GCNConv(MessagePassing):
    """
    Extension of the Graph Convolutional Network to incorporate
    edge information by concatenating edge embeddings.
    """

    def __init__(self, emb_dim, aggr="add"):
        super(GCNConv, self).__init__(aggr=aggr)

        self.linear = torch.nn.Linear(emb_dim, emb_dim)
        self.edge_embedding1 = torch.nn.Embedding(num_bond_type, emb_dim)
        self.edge_embedding2 = torch.nn.Embedding(num_bond_direction, emb_dim)

        torch.nn.init.xavier_uniform_(self.edge_embedding1.weight.data)
        torch.nn.init.xavier_uniform_(self.edge_embedding2.weight.data)

    @staticmethod
    def norm(edge_index, num_nodes, dtype):

        # symmetrically normalise edge weights
        edge_weight = torch.ones((edge_index.size(1),), dtype=dtype, device=edge_index.device)
        row, col = edge_index
        deg = scatter_add(edge_weight, row, dim=0, dim_size=num_nodes)
        deg_inv_sqrt = deg.pow(-0.5)
        deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0
        return deg_inv_sqrt[row] * edge_weight * deg_inv_sqrt[col]

    def forward(self, x, edge_index, edge_attr):

        # add self loops to edge index
        edge_index, _ = add_self_loops(edge_index, num_nodes=x.size(0))

        # add features corresponding to self-loop edges.
        self_loop_attr = torch.zeros(x.size(0), 2)
        self_loop_attr[:, 0] = self_loop_token
        self_loop_attr = self_loop_attr.to(edge_attr.device).to(edge_attr.dtype)
        edge_attr = torch.cat((edge_attr, self_loop_attr), dim=0)

        # generate edge embeddings and norm, and propagate
        edge_embeddings = self.edge_embedding1(edge_attr[:, 0]) + self.edge_embedding2(edge_attr[:, 1])
        return self.propagate(
            edge_index,
            x=self.linear(x),
            edge_attr=edge_embeddings,
            norm=self.norm(edge_index, x.size(0), x.dtype)
        )

    def message(self, x_j, edge_attr, norm):
        return norm.view(-1, 1) * (x_j + edge_attr)


class GNN(torch.nn.Module):
    """
    Combine multiple GNN layers into a network.
    """

    def __init__(self, args):

        super(GNN, self).__init__()
        self.args = args

        if self.args.num_layer < 2:
            raise ValueError("Number of GNN layers must be greater than 1.")

        # initialise label embeddings
        self.x_embedding1 = torch.nn.Embedding(num_atom_type, self.args.emb_dim)
        self.x_embedding2 = torch.nn.Embedding(num_chirality_tag, self.args.emb_dim)
        torch.nn.init.xavier_uniform_(self.x_embedding1.weight.data)
        torch.nn.init.xavier_uniform_(self.x_embedding2.weight.data)

        # initialise GNN layers
        self.gnns = torch.nn.ModuleList()
        for layer in range(self.args.num_layer):
            if self.args.gnn_type == "gin":
                self.gnns.append(GINConv(emb_dim=self.args.emb_dim))
            elif self.args.gnn_type == "gcn":
                self.gnns.append(GCNConv(emb_dim=self.args.emb_dim))
            else:
                raise NotImplementedError('Invalid GNN layer type.')

        # initialise BatchNorm layers
        self.batch_norms = torch.nn.ModuleList()
        for layer in range(self.args.num_layer):
            self.batch_norms.append(torch.nn.BatchNorm1d(self.args.emb_dim))

    def forward(self, x, edge_index, edge_attr):

        # x[:, 0] corresponds to 'possible_atomic_num_list',
        # x[:, 1] corresponds to 'possible_chirality_list'
        x = self.x_embedding1(x[:, 0]) + self.x_embedding2(x[:, 1])

        for layer in range(self.args.num_layer):
            # x are atom features of the molecule and edge_attr the atomic features of the molecule
            x = self.gnns[layer](x, edge_index, edge_attr)
            x = self.batch_norms[layer](x)
            if layer != self.args.num_layer - 1:
                x = F.relu(x)
            x = F.dropout(x, self.args.dropout_ratio, training=self.training)

        return x


if __name__ == "__main__":
    pass
