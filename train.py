import os
import torch
import matplotlib.pyplot as plt
import numpy as np
import torch.nn.functional as F
import torchvision.transforms as T

from skimage.io import imread

# Util function for loading meshes
from pytorch3d.io import load_objs_as_meshes, load_obj

# Data structures and functions for rendering
from pytorch3d.structures import Meshes
from pytorch3d.renderer import (
    look_at_view_transform,
    FoVPerspectiveCameras,
    OpenGLPerspectiveCameras,
    PointLights,
    DirectionalLights,
    Materials,
    RasterizationSettings,
    MeshRenderer,
    MeshRasterizer,
    SoftPhongShader,
    HardPhongShader,
    TexturesUV,
    BlendParams,
    SoftSilhouetteShader
)

import sys
import os

from MeshDataset import MeshDataset
from BackgroundDataset import BackgroundDataset
from darknet import Darknet
from loss import TotalVariation, dis_loss, calc_acc

from torchvision.utils import save_image

import random
import torchvision
from PIL import ImageDraw
from faster_rcnn.dataset.base import Base as DatasetBase
from faster_rcnn.backbone.base import Base as BackboneBase
from faster_rcnn.bbox import BBox
from faster_rcnn.model import Model as FasterRCNN
from faster_rcnn.roi.pooler import Pooler
from faster_rcnn.config.eval_config import EvalConfig as Config

class Patch():
    def __init__(self, config, device):
        self.config = config
        self.device = device

        # Create pytorch3D renderer
        self.renderer = self.create_renderer()

        # Datasets
        self.mesh_dataset = MeshDataset(config.mesh_dir, device)
        self.bg_dataset = BackgroundDataset(config.bg_dir, config.img_size, max_num=config.num_bgs)
        self.test_bg_dataset = BackgroundDataset(config.test_bg_dir, config.img_size, max_num=config.num_test_bgs)

        # Initialize adversarial patch, and TV loss
        self.patch = torch.rand((100, 100, 3), device=device, requires_grad=True)
        self.total_variation = TotalVariation().to(device)

        # Yolo model:
        self.dnet = Darknet(self.config.cfgfile)
        self.dnet.load_weights(self.config.weightfile)
        self.dnet = self.dnet.eval()
        self.dnet = self.dnet.to(self.device)

    def attack(self):
        optimizer = torch.optim.SGD([self.patch], lr=1.0, momentum=0.9)

        for epoch in range(self.config.epochs):
            ep_loss = 0.0
            ep_acc = 0.0
            n = 0.0

            for mesh in self.mesh_dataset:
                # Copy mesh for each camera angle
                mesh = mesh.extend(self.num_angles)
                mesh_texture = mesh.textures.maps_padded()

                for bg in self.bg_dataset:
                    optimizer.zero_grad()

                    # Apply patch to mesh texture (hard coded for now)
                    mesh_texture[:, 575:675, 475:575, :] = self.patch[None]

                    # Render mesh onto background image
                    images = self.render_mesh_on_bg(mesh, bg)
                    #images[:, 100:200, 100:200, :] = self.patch[None]
                    reshape_img = images[:,:,:,:3].permute(0, 3, 1, 2)
                    reshape_img = reshape_img.to(self.device)

                    # Run detection model on images
                    output = self.dnet(reshape_img)

                    # Compute losses:
                    d_loss = dis_loss(output, self.dnet.num_classes, self.dnet.anchors, self.dnet.num_anchors, 0)
                    acc_loss = calc_acc(output, self.dnet.num_classes, self.dnet.num_anchors, 0)

                    tv = self.total_variation(self.patch)
                    tv_loss = tv * 2.5

                    #loss = d_loss + torch.sum(torch.max(tv_loss, torch.tensor(0.1).to(self.device)))
                    loss = torch.sum(torch.max(tv_loss, torch.tensor(0.1).to(self.device)))

                    ep_loss += loss.item()
                    ep_acc += acc_loss
                    n += 1.0

                    # TODO: need to remove retain_graph
                    d_loss.backward(retain_graph=True)
                    loss.backward()
                    optimizer.step()

            # Save image and print performance statistics
            save_image(self.patch.cpu().detach().permute(2, 0, 1), self.config.output + '_{}.png'.format(epoch))
            print('epoch={} loss={} success_rate={}'.format(epoch, ep_loss / n, (ep_acc / n) / self.num_angles))
            self.test_patch()

    def test_patch(self):
        angle_success = torch.zeros(self.num_angles)
        total_loss = 0.0
        n = 0.0
        with torch.no_grad():
            for mesh in self.mesh_dataset:
                mesh = mesh.extend(self.num_angles)
                mesh_texture = mesh.textures.maps_padded()
                for bg in self.test_bg_dataset:

                    mesh_texture[:, 575:675, 475:575, :] = self.patch[None]

                    images = self.render_mesh_on_bg(mesh, bg)
                    reshape_img = images[:,:,:,:3].permute(0, 3, 1, 2)
                    reshape_img = reshape_img.to(self.device)
                    output = self.dnet(reshape_img)

                    d_loss = dis_loss(output, self.dnet.num_classes, self.dnet.anchors, self.dnet.num_anchors, 0)

                    for angle in range(self.num_angles):
                        acc_loss = calc_acc(output[angle], self.dnet.num_classes, self.dnet.num_anchors, 0)
                        angle_success[angle] += acc_loss.item()

                    tv = self.total_variation(self.patch)
                    tv_loss = tv * 2.5

                    loss = d_loss + torch.sum(torch.max(tv_loss, torch.tensor(0.1).to(self.device)))

                    total_loss += loss.item()
                    n += 1.0

        unseen_success_rate = angle_success.mean() / len(self.test_bg_dataset)
        print('Unseen bg success rate: ', unseen_success_rate.item())

    def create_renderer(self):
        self.num_angles = self.config.num_angles
        azim = torch.linspace(-10, 10, self.num_angles)

        R, T = look_at_view_transform(dist=1.0, elev=0, azim=azim)

        T[:, 1] = -85
        T[:, 2] = 200

        cameras = FoVPerspectiveCameras(device=self.device, R=R, T=T)

        raster_settings = RasterizationSettings(
            image_size=self.config.img_size,
            blur_radius=0.0,
            faces_per_pixel=1,
        )

        lights = PointLights(device=self.device, location=[[0.0, 85, 100.0]])

        renderer = MeshRenderer(
            rasterizer=MeshRasterizer(
                cameras=cameras,
                raster_settings=raster_settings
            ),
            shader=HardPhongShader(
                device=self.device,
                cameras=cameras,
                lights=lights
            )
        )
        return renderer

    def render_mesh_on_bg(self, mesh, bg_img, location=None, x_translation=0, y_translation=0):
        images = self.renderer(mesh)
        bg = bg_img.unsqueeze(0)
        bg_shape = bg.shape
        new_bg = torch.zeros(bg_shape[2], bg_shape[3], 3)
        new_bg[:,:,0] = bg[0,0,:,:]
        new_bg[:,:,1] = bg[0,1,:,:]
        new_bg[:,:,2] = bg[0,2,:,:]

        human = images[:, ..., :3]

        human_size = self.renderer.rasterizer.raster_settings.image_size

        if location is None:
            dH = bg_shape[2] - human_size
            dW = bg_shape[3] - human_size
            location = (
                dW // 2 + x_translation,
                dW - (dW // 2) - x_translation,
                dH // 2 + y_translation,
                dH - (dH // 2) - y_translation
            )

        contour = torch.where((human == 1).cpu(), torch.zeros(1).cpu(), torch.ones(1).cpu())
        new_contour = torch.zeros(self.num_angles, bg_shape[2], bg_shape[3], 3)

        new_contour[:,:,:,0] = F.pad(contour[:,:,:,0], location, "constant", value=0)
        new_contour[:,:,:,1] = F.pad(contour[:,:,:,1], location, "constant", value=0)
        new_contour[:,:,:,2] = F.pad(contour[:,:,:,2], location, "constant", value=0)

        new_human = torch.zeros(self.num_angles, bg_shape[2], bg_shape[3], 3)
        new_human[:,:,:,0] = F.pad(human[:,:,:,0], location, "constant", value=0)
        new_human[:,:,:,1] = F.pad(human[:,:,:,1], location, "constant", value=0)
        new_human[:,:,:,2] = F.pad(human[:,:,:,2], location, "constant", value=0)

        final = torch.where((new_contour == 0).cpu(), new_bg.cpu(), new_human.cpu())
        return final.cuda()


def main():
    import argparse
    parser = argparse.ArgumentParser()


    parser.add_argument('--data_path', type=str, default='data')
    parser.add_argument('--mesh_dir', type=str, default='data/meshes')
    parser.add_argument('--bg_dir', type=str, default='data/background')
    parser.add_argument('--test_bg_dir', type=str, default='data/test_background')
    parser.add_argument('--output', type=str, default='out/patch')

    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--img_size', type=int, default=416)
    parser.add_argument('--num_bgs', type=int, default=20)
    parser.add_argument('--num_test_bgs', type=int, default=30)
    parser.add_argument('--num_angles', type=int, default=10)

    parser.add_argument('--cfgfile', type=str, default="cfg/yolo.cfg")
    parser.add_argument('--weightfile', type=str, default="weights/yolo.weights")

    # Set different devices
    parser.add_argument('--gpu_id', type=int, default=0)

    config = parser.parse_args()

    if torch.cuda.is_available():
        device = torch.device("cuda:%d" % config.gpu_id)
        torch.cuda.set_device(device)
    else:
        device = torch.device("cpu")

    trainer = Patch(config, device)

    trainer.attack()

if __name__ == '__main__':
    main()
