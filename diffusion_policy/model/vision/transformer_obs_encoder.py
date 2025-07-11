import copy

import timm
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import logging

from diffusion_policy.model.common.module_attr_mixin import ModuleAttrMixin

from diffusion_policy.common.pytorch_util import replace_submodules

logger = logging.getLogger(__name__)

def make_viridis_colormap():
    """
    Returns a (256, 3) float Tensor in [0,1], which is
    the standard 'viridis' colormap from matplotlib.
    """
    import matplotlib
    from matplotlib import cm
    viridis = cm.get_cmap('viridis', 256)
    # viridis(range(256)) -> shape (256,4), but last channel is alpha
    colormap = viridis(np.arange(256))[:, :3]  # take RGB
    colormap = torch.FloatTensor(colormap)     # shape [256, 3], in [0,1]
    return colormap

class AttentionPool2d(nn.Module):
    def __init__(self, spacial_dim: int, embed_dim: int, num_heads: int, output_dim: int = None):
        super().__init__()
        self.positional_embedding = nn.Parameter(torch.randn(spacial_dim ** 2 + 1, embed_dim) / embed_dim ** 0.5)
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.c_proj = nn.Linear(embed_dim, output_dim or embed_dim)
        self.num_heads = num_heads

    def forward(self, x):
        x = x.flatten(start_dim=2).permute(2, 0, 1)  # NCHW -> (HW)NC
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)  # (HW+1)NC
        x = x + self.positional_embedding[:, None, :].to(x.dtype)  # (HW+1)NC
        x, _ = F.multi_head_attention_forward(
            query=x[:1], key=x, value=x,
            embed_dim_to_check=x.shape[-1],
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False
        )
        return x.squeeze(0)
    

# ADDED: A simple CNN for tactile data
class SimpleCNN(nn.Module):
    def __init__(self, out_dim=768):
        super().__init__()
        self.conv1 = nn.Conv2d(3, 16, kernel_size=3, stride=2, padding=1)
        self.bn1   = nn.BatchNorm2d(16)
        self.conv2 = nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1)
        self.bn2   = nn.BatchNorm2d(32)
        self.conv3 = nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1)
        self.bn3   = nn.BatchNorm2d(64)
        
        # AdaptiveAvgPool2d squeezes the feature map to size (1x1),
        # making the feature dimension = 64
        self.pool  = nn.AdaptiveAvgPool2d((1,1))

        # out_dim is the size of your final embedding
        self.fc    = nn.Linear(64, out_dim)

    def forward(self, x):
        # x shape: (B, 3, 224, 224), or any other large resolution
        x = F.relu(self.bn1(self.conv1(x)))  # (B,16, H/2,   W/2)
        x = F.relu(self.bn2(self.conv2(x)))  # (B,32, H/4,   W/4)
        x = F.relu(self.bn3(self.conv3(x)))  # (B,64, H/8,   W/8)
        x = self.pool(x)                     # (B,64, 1,1)
        x = x.view(x.size(0), -1)            # (B,64)
        x = self.fc(x)                       # (B,out_dim)
        return x
        
class TransformerObsEncoder(ModuleAttrMixin):
    def __init__(self,
            shape_meta: dict,
            model_name: str='vit_base_patch16_clip_224.openai',
            global_pool: str='',
            transforms: list=None,
            use_tactile: bool=True,
            n_emb: int=768,
            pretrained: bool=False,
            frozen: bool=False,
            # replace BatchNorm with GroupNorm
            use_group_norm: bool=False,
            # use single rgb model for all rgb inputs
            share_rgb_model: bool=False,
            feature_aggregation: str=None,
            downsample_ratio: int=32,

            tactile_model_choice: str='simple_cnn', 
        ):
        """
        Assumes rgb input: B,T,C,H,W
        Assumes low_dim input: B,T,D
        """
        super().__init__()

        self.use_tactile = use_tactile
        print("use_tactile",self.use_tactile)
        if self.use_tactile:
            print("tactile_model_choice", tactile_model_choice)
        
        rgb_keys = list()
        low_dim_keys = list()
        tactile_keys = list()
        key_model_map = nn.ModuleDict()
        key_transform_map = nn.ModuleDict()
        key_projection_map = nn.ModuleDict()
        key_shape_map = dict()

        assert global_pool == ''
        model = timm.create_model(
            model_name=model_name,
            pretrained=pretrained,
            global_pool=global_pool, # '' means no pooling
            num_classes=0            # remove classification layer
        )
        self.model_name = model_name
        if self.use_tactile:
            # CHANGED: tact_model can be either ResNet18 or SimpleCNN
            if tactile_model_choice == 'resnet18':
                tactile_model = timm.create_model(
                    model_name="resnet18",
                    pretrained=False,
                    global_pool=global_pool,
                    num_classes=0
                )
            elif tactile_model_choice == 'simple_cnn':
                tactile_model = SimpleCNN(out_dim=768)
            else:
                raise ValueError(f"Unknown tactile_model_choice: {tactile_model_choice}")


        if frozen:
            assert pretrained
            for param in model.parameters():
                param.requires_grad = False
        
        feature_dim = None
        if model_name.startswith('resnet'):
            # the last layer is nn.Identity() because num_classes is 0
            # second last layer is AdaptivePool2d, which is also identity because global_pool is empty
            if downsample_ratio == 32:
                modules = list(model.children())[:-2]
                model = torch.nn.Sequential(*modules)
                feature_dim = 512
            elif downsample_ratio == 16:
                modules = list(model.children())[:-3]
                model = torch.nn.Sequential(*modules)
                feature_dim = 256
            else:
                raise NotImplementedError(f"Unsupported downsample_ratio: {downsample_ratio}")
        elif model_name.startswith('convnext'):
            # the last layer is nn.Identity() because num_classes is 0
            # second last layer is AdaptivePool2d, which is also identity because global_pool is empty
            if downsample_ratio == 32:
                modules = list(model.children())[:-2]
                model = torch.nn.Sequential(*modules)
                feature_dim = 1024
            else:
                raise NotImplementedError(f"Unsupported downsample_ratio: {downsample_ratio}")

        if use_group_norm and not pretrained:
            model = replace_submodules(
                root_module=model,
                predicate=lambda x: isinstance(x, nn.BatchNorm2d),
                func=lambda x: nn.GroupNorm(
                    num_groups=(x.num_features // 16) if (x.num_features % 16 == 0) else (x.num_features // 8), 
                    num_channels=x.num_features)
            )
            
        # handle feature aggregation
        self.feature_aggregation = feature_aggregation
        if model_name.startswith('vit'):
            # assert self.feature_aggregation is None # vit uses the CLS token
            if self.feature_aggregation is None:
                # Use all tokens from ViT
                pass
            elif self.feature_aggregation != 'cls':
                logger.warn(f'vit will use the CLS token. feature_aggregation ({self.feature_aggregation}) is ignored!')
                self.feature_aggregation = 'cls'
        
        if self.feature_aggregation == 'soft_attention':
            self.attention = nn.Sequential(
                nn.Linear(feature_dim, 1, bias=False),
                nn.Softmax(dim=1)
            )
        elif self.feature_aggregation == 'spatial_embedding':
            self.spatial_embedding = torch.nn.Parameter(torch.randn(feature_map_shape[0] * feature_map_shape[1], feature_dim))
        elif self.feature_aggregation == 'attention_pool_2d':
            self.attention_pool_2d = AttentionPool2d(
                spacial_dim=feature_map_shape[0],
                embed_dim=feature_dim,
                num_heads=feature_dim // 64,
                output_dim=feature_dim
            )
        if self.use_tactile:
            # We store it as a buffer so it moves automatically with .to(device)
            self.register_buffer(
                "viridis_map",
                make_viridis_colormap(),  # shape [256, 3], in [0,1]
                persistent=False
            )
        
        image_shape = None
        obs_shape_meta = shape_meta['obs']
        for key, attr in obs_shape_meta.items():
            shape = tuple(attr['shape'])
            type = attr.get('type', 'low_dim')
            if type == 'rgb':
                assert image_shape is None or image_shape == shape[1:]
                image_shape = shape[1:]
        if transforms is not None and not isinstance(transforms[0], torch.nn.Module):
            assert transforms[0].type == 'RandomCrop'
            ratio = transforms[0].ratio
            transforms = [
                torchvision.transforms.RandomCrop(size=int(image_shape[0] * ratio)),
                torchvision.transforms.Resize(size=image_shape[0], antialias=True)
            ] + transforms[1:]
        transform = nn.Identity() if transforms is None else torch.nn.Sequential(*transforms)

        for key, attr in obs_shape_meta.items():
            shape = tuple(attr['shape'])
            type = attr.get('type', 'low_dim')
            key_shape_map[key] = shape
            if type == 'rgb':
                rgb_keys.append(key)

                this_model = model if share_rgb_model else copy.deepcopy(model)
                key_model_map[key] = this_model
                
                # check if we need feature projection
                with torch.no_grad():
                    example_img = torch.zeros((1,)+tuple(shape))
                    example_feature_map = this_model(example_img)
                    example_features = self.aggregate_feature(example_feature_map)
                    feature_shape = example_features.shape
                    feature_size = feature_shape[-1]
                proj = nn.Identity()
                if feature_size != n_emb:
                    proj = nn.Linear(in_features=feature_size, out_features=n_emb)
                key_projection_map[key] = proj

                this_transform = transform
                key_transform_map[key] = this_transform
            elif type == 'low_dim':
                dim = np.prod(shape)
                proj = nn.Identity()
                if dim != n_emb:
                    proj = nn.Linear(in_features=dim, out_features=n_emb)
                key_projection_map[key] = proj

                low_dim_keys.append(key)
            elif type == 'tactile':
                if self.use_tactile:
                    tactile_keys.append(key)
                    this_tactile_model = tactile_model
                    key_model_map[key] = this_tactile_model
                    key_transform_map[key] = transform
            else:
                raise RuntimeError(f"Unsupported obs type: {type}")
        
        feature_map_shape = [x // downsample_ratio for x in image_shape]
            
        rgb_keys = sorted(rgb_keys)
        low_dim_keys = sorted(low_dim_keys)

        self.n_emb = n_emb
        self.shape_meta = shape_meta
        self.key_model_map = key_model_map
        self.key_transform_map = key_transform_map
        self.key_projection_map = key_projection_map
        self.share_rgb_model = share_rgb_model
        self.rgb_keys = rgb_keys
        self.low_dim_keys = low_dim_keys
        self.tactile_keys = tactile_keys
        self.key_shape_map = key_shape_map

        logger.info(
            "number of parameters: %e", sum(p.numel() for p in self.parameters())
        )

    def aggregate_feature(self, feature):
        # Return: B, N, C
        
        if self.model_name.startswith('vit'):
            # vit uses the CLS token
            if self.feature_aggregation == 'cls':
                return feature[:, [0], :]
            
            # or use all tokens
            assert self.feature_aggregation is None 
            return feature
        
        # resnet
        assert len(feature.shape) == 4
        if self.feature_aggregation == 'attention_pool_2d':
            return self.attention_pool_2d(feature)

        feature = torch.flatten(feature, start_dim=-2) # B, 512, 7*7
        feature = torch.transpose(feature, 1, 2) # B, 7*7, 512

        if self.feature_aggregation == 'avg':
            return torch.mean(feature, dim=[1], keepdim=True)
        elif self.feature_aggregation == 'max':
            return torch.amax(feature, dim=[1], keepdim=True)
        elif self.feature_aggregation == 'soft_attention':
            weight = self.attention(feature)
            return torch.sum(feature * weight, dim=1, keepdim=True)
        elif self.feature_aggregation == 'spatial_embedding':
            return torch.mean(feature * self.spatial_embedding, dim=1, keepdim=True)
        else:
            assert self.feature_aggregation is None
            return feature
        
    def forward(self, obs_dict):
        embeddings = list()
        batch_size = next(iter(obs_dict.values())).shape[0]
        
        # process rgb input
        for key in self.rgb_keys:
            img = obs_dict[key]
            B, T = img.shape[:2]
            assert B == batch_size
            assert img.shape[2:] == self.key_shape_map[key]
            img = img.reshape(B*T, *img.shape[2:])
            img = self.key_transform_map[key](img)
            raw_feature = self.key_model_map[key](img)
            feature = self.aggregate_feature(raw_feature)
            emb = self.key_projection_map[key](feature)
            assert len(emb.shape) == 3 and emb.shape[0] == B * T and emb.shape[-1] == self.n_emb
            emb = emb.reshape(B,-1,self.n_emb)
            embeddings.append(emb)

        # process lowdim input
        for key in self.low_dim_keys:
            data = obs_dict[key]
            B, T = data.shape[:2]
            assert B == batch_size
            assert data.shape[2:] == self.key_shape_map[key]
            data = data.reshape(B,T,-1)
            emb = self.key_projection_map[key](data)
            assert emb.shape[-1] == self.n_emb
            embeddings.append(emb)

        if self.use_tactile:
            for key in self.tactile_keys:
                tactile_data = obs_dict[key]
                #print("Tactile data shape:", tactile_data.shape)
                # tactile_data shape: (B, T, 12, 64)
                B, T = tactile_data.shape[:2]
                assert B == batch_size
                # Reshape to (B*T, 12, 64)
                tactile_data = tactile_data.reshape(B * T, *tactile_data.shape[2:])
                # Split along the width: left (first 32 columns) and right (last 32 columns)
                left_tactile = tactile_data[:, :, :32]   # shape: (B*T, 12, 32)
                right_tactile = tactile_data[:, :, 32:]    # shape: (B*T, 12, 32)

                # ---- GPU-based color mapping with our viridis buffer ----
                # 1) clamp to [0, 1] in case it isn't already
                left_tactile = left_tactile.clamp(0, 1)
                right_tactile = right_tactile.clamp(0, 1)

                # 2) scale to [0, 255], convert to long for indexing
                left_index  = (left_tactile * 255).long().clamp(0, 255)
                right_index = (right_tactile * 255).long().clamp(0, 255)

                # 3) map each pixel to [R,G,B] from self.viridis_map (shape [256, 3])
                # left_color: shape (B*T, 12, 32, 3)
                left_color = self.viridis_map[left_index]
                right_color = self.viridis_map[right_index]
                # 4) combine along width → (B*T, 24, 32, 3)
                tactile_images = torch.cat([left_color, right_color], dim=1)

                # 5) permute to CHW: (B*T, 3, 24, 32)
                tactile_images = tactile_images.permute(0, 3, 1, 2)

                '''
                # ─── SAVE COLORED TACTILE IMAGES ────────────────────────────────────────────────
                import os
                save_dir = "tactile_debug"
                os.makedirs(save_dir, exist_ok=True)

                # tactile_images_np shape = (B*T, 12, 64, 3)
                for idx in range(min(8, tactile_images_np.shape[0])):   # save up to 8
                    img = tactile_images_np[idx]                         # uint8 H×W×3
                    out_path = os.path.join(save_dir, f"{key}_sample{idx}.png")
                    cv2.imwrite(out_path, img[..., ::-1])                # BGR→RGB flip for cv2
                    logger.info(f"Saved tactile image → {out_path}")
                # ───────────────────────────────────────────────────────────────────────────────
                '''

                '''
                # Resize tactile images to meet the crop requirements, e.g., (224, 224)
                tactile_images = F.interpolate(
                    tactile_images, 
                    size=(224, 224), 
                    mode='bilinear', 
                    align_corners=False
                )

                # Optionally, apply any additional transforms registered for tactile data
                tactile_images = self.key_transform_map[key](tactile_images)
                '''

                device = next(self.key_model_map[key].parameters()).device
                tactile_images = tactile_images.to(device)
                # Process through the tactile model (originally a ResNet, but might be SimpleCNN)
                raw_feature = self.key_model_map[key](tactile_images)

                assert raw_feature.dim() == 2
                assert raw_feature.size(0) == B * T

                emb = raw_feature.reshape(B, T, self.n_emb)
                embeddings.append(emb)  # Keep it 3D

                
        
        # concatenate all features along t
        result = torch.cat(embeddings, dim=1)
        return result

    @torch.no_grad()
    def output_shape(self):
        example_obs_dict = dict()
        obs_shape_meta = self.shape_meta['obs']
        for key, attr in obs_shape_meta.items():
            shape = tuple(attr['shape'])
            this_obs = torch.zeros(
                (1, attr['horizon']) + shape, 
                dtype=self.dtype,
                device=self.device)
            example_obs_dict[key] = this_obs
        example_output = self.forward(example_obs_dict)
        assert len(example_output.shape) == 3
        assert example_output.shape[0] == 1

        return example_output.shape


def test():
    import hydra
    from omegaconf import OmegaConf
    OmegaConf.register_new_resolver("eval", eval, replace=True)

    with hydra.initialize('../diffusion_policy/config'):
        cfg = hydra.compose('train_diffusion_transformer_umi_workspace')
        OmegaConf.resolve(cfg)

    shape_meta = cfg.task.shape_meta
    encoder = TransformerObsEncoder(
        shape_meta=shape_meta
    )
