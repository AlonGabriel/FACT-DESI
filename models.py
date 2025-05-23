from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import CLIPConfig, CLIPModel
from transformers import ClapConfig, ClapModel

from dreams import DreaMS


@dataclass
class ModelOutputs:
    raw_outputs: torch.FloatTensor
    embeddings: Optional[torch.FloatTensor] = None
    predictions: Optional[torch.FloatTensor] = None


class FoundationModel(nn.Module, ABC):

    def __init__(self, backbone, proj_head=None, pred_head=None):
        super().__init__()
        self.backbone = backbone
        self.proj_head = proj_head
        self.pred_head = pred_head

    def forward(self, x):
        raw_outputs, embeddings = self.forward_backbone(x)
        if self.pred_head is None:
            return ModelOutputs(raw_outputs=raw_outputs, embeddings=embeddings)
        predictions = self.pred_head(embeddings if embeddings is not None else raw_outputs)
        return ModelOutputs(raw_outputs=raw_outputs, embeddings=embeddings, predictions=predictions)

    @abstractmethod
    def forward_backbone(self, x):
        pass

    @staticmethod
    @abstractmethod
    def from_base_model(base_model, projection_head, random_init, prediction_head):
        pass

    @staticmethod
    def mlp_head(d_input, hidden_channels, norm_layer=nn.BatchNorm1d, activation_layer=nn.ReLU, dropout=0.5, bias=True):
        layers = []
        in_dim = d_input
        for hidden_dim in hidden_channels[:-1]:
            layers.append(nn.Linear(in_dim, hidden_dim, bias=bias))
            if norm_layer is not None:
                layers.append(norm_layer(hidden_dim))
            layers.append(activation_layer())
            layers.append(nn.Dropout(dropout))
            in_dim = hidden_dim
        layers.append(nn.Linear(in_dim, hidden_channels[-1], bias=bias))
        return nn.Sequential(*layers)


class CLAPBasedModel(FoundationModel):

    @staticmethod
    def from_base_model(base_model, projection_head, random_init, prediction_head, reinit_proj_head=False):
#         clap = ClapModel(ClapConfig.from_pretrained(base_model)) if random_init else ClapModel.from_pretrained(base_model)
#         proj_head = clap.audio_projection if projection_head else None
#         backbone = clap.audio_model
#         d_input = backbone.config.projection_dim if projection_head else backbone.config.hidden_size
#         pred_head = FoundationModel.mlp_head(d_input, prediction_head) if prediction_head else None
#         return CLAPBasedModel(backbone, proj_head, pred_head)
        clap = ClapModel(ClapConfig.from_pretrained(base_model)) if random_init else ClapModel.from_pretrained(base_model)
        proj_head = clap.audio_projection if projection_head else None
        if reinit_proj_head and proj_head is not None:
            print("Reinitializing projection head...")
            # Reinitialize the layers in projection head
            proj_head.linear1 = nn.Linear(proj_head.linear1.in_features, proj_head.linear1.out_features, bias=True)
            proj_head.linear2 = nn.Linear(proj_head.linear2.in_features, proj_head.linear2.out_features, bias=True)

        backbone = clap.audio_model
        d_input = backbone.config.projection_dim if projection_head else backbone.config.hidden_size
        pred_head = FoundationModel.mlp_head(d_input, prediction_head) if prediction_head else None

        return CLAPBasedModel(backbone, proj_head, pred_head)

    def forward_backbone(self, x):
        n, m = x.shape
        d_input = self.backbone.config.num_mel_bins
        multiple = (m // d_input) * d_input
        x = x[:, :multiple].reshape(n, 1, -1, d_input)
        raw_outputs = self.backbone(x).pooler_output
        proj_embeds = None
        if self.proj_head is not None:
            proj_embeds = self.proj_head(raw_outputs)
            proj_embeds = F.normalize(proj_embeds)
        return raw_outputs, proj_embeds


class CLIPBasedModel(FoundationModel):

    @staticmethod
    def from_base_model(base_model, projection_head, random_init, prediction_head):
        clip = CLIPModel(CLIPConfig.from_pretrained(base_model)) if random_init else CLIPModel.from_pretrained(base_model)
        proj_head = clip.visual_projection if projection_head else None
        backbone = clip.vision_model
        d_input = backbone.config.projection_dim if projection_head else backbone.config.hidden_size
        pred_head = FoundationModel.mlp_head(d_input, prediction_head) if prediction_head else None
        return CLIPBasedModel(backbone, proj_head, pred_head)

    def forward_backbone(self, x):
        raw_outputs = self.backbone(x).pooler_output
        proj_embeds = None
        if self.proj_head is not None:
            proj_embeds = self.proj_head(raw_outputs)
            proj_embeds = F.normalize(proj_embeds)
        return raw_outputs, proj_embeds


class DreaMSBasedModel(FoundationModel):

    @staticmethod
    def from_base_model(base_model, projection_head, random_init, prediction_head, reinit_proj_head=False):
        backbone = DreaMS()
        d_model = backbone.d_model
        proj_head = nn.Linear(d_model, d_model)
        pred_head = FoundationModel.mlp_head(d_model, prediction_head) if prediction_head else None
        model = DreaMSBasedModel(backbone, proj_head, pred_head)
        if not random_init:
            state_dict = torch.load('checkpoints/DreaMS.pt')
            model.load_state_dict(state_dict, strict=False)
        return model

    def forward_backbone(self, x):
        x = self.top_n_mz(x)
        raw_outputs = self.backbone(x)
        raw_outputs = raw_outputs[:, 0]  # Precursor
        proj_embeds = None
        if self.proj_head is not None:
            proj_embeds = self.proj_head(raw_outputs)
        return raw_outputs, proj_embeds

    def top_n_mz(self, x):
        top_n = self.backbone.top_n + 1  # Precursor
        with torch.no_grad():
            x, i = torch.sort(x, descending=True)
            i = i + 100  # Our spectra range from 100 to 1000
            stacked = torch.stack((i, x), dim=-1)
            return stacked[:, :top_n, :]


construct = {
    'laion/clap-htsat-unfused': CLAPBasedModel,
    'openai/clip-vit-base-patch32': CLIPBasedModel,
    'pluskal-lab/DreaMS': DreaMSBasedModel,
}


def construct_model(base_model, projection_head, random_init, prediction_head, reinit_proj_head=False):
    return construct[base_model].from_base_model(base_model, projection_head, random_init, prediction_head, reinit_proj_head)
