import os
import math
import cv2
import trimesh
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import nvdiffrast.torch as dr
from mesh import Mesh, safe_normalize

def scale_img_nhwc(x, size, mag='bilinear', min='bilinear'):
    assert (x.shape[1] >= size[0] and x.shape[2] >= size[1]) or (x.shape[1] < size[0] and x.shape[2] < size[1]), "Trying to magnify image in one dimension and minify in the other"
    y = x.permute(0, 3, 1, 2) # NHWC -> NCHW
    if x.shape[1] > size[0] and x.shape[2] > size[1]: # Minification, previous size was bigger
        y = torch.nn.functional.interpolate(y, size, mode=min)
    else: # Magnification
        if mag == 'bilinear' or mag == 'bicubic':
            y = torch.nn.functional.interpolate(y, size, mode=mag, align_corners=True)
        else:
            y = torch.nn.functional.interpolate(y, size, mode=mag)
    return y.permute(0, 2, 3, 1).contiguous() # NCHW -> NHWC

def scale_img_hwc(x, size, mag='bilinear', min='bilinear'):
    return scale_img_nhwc(x[None, ...], size, mag, min)[0]

def scale_img_nhw(x, size, mag='bilinear', min='bilinear'):
    return scale_img_nhwc(x[..., None], size, mag, min)[..., 0]

def scale_img_hw(x, size, mag='bilinear', min='bilinear'):
    return scale_img_nhwc(x[None, ..., None], size, mag, min)[0, ..., 0]

def trunc_rev_sigmoid(x, eps=1e-6):
    x = x.clamp(eps, 1 - eps)
    return torch.log(x / (1 - x))

def make_divisible(x, m=8):
    return int(math.ceil(x / m) * m)

class Renderer(nn.Module):
    def __init__(self, opt):
        
        super().__init__()

        self.opt = opt

        self.mesh = Mesh.load(self.opt.mesh)

        if self.opt.wogui or os.name == 'nt':
            self.glctx = dr.RasterizeGLContext()
        else:
            self.glctx = dr.RasterizeCudaContext()


    @torch.no_grad()
    def export_mesh(self, save_path):
        self.mesh.write(save_path)

    def render(self, mvp, h0, w0, ssaa=1, bg_color=1):
        # mvp: [4, 4]

        # do super-sampling
        if ssaa != 1:
            h = make_divisible(h0 * ssaa, 8)
            w = make_divisible(w0 * ssaa, 8)
        else:
            h, w = h0, w0
        
        results = {}

        # get v
        v = self.mesh.v

        # get v_clip and render rgb
        v_clip = torch.matmul(F.pad(v, pad=(0, 1), mode='constant', value=1.0), torch.transpose(mvp, 0, 1)).float().unsqueeze(0) # [1, N, 4]

        rast, rast_db = dr.rasterize(self.glctx, v_clip, self.mesh.f, (h, w))

        alpha = (rast[..., 3:] > 0).float()

        texc, texc_db = dr.interpolate(self.mesh.vt.unsqueeze(0).contiguous(), rast, self.mesh.ft, rast_db=rast_db, diff_attrs='all')
        albedo = dr.texture(self.mesh.albedo.unsqueeze(0), texc, uv_da=texc_db, filter_mode='linear-mipmap-linear') # [1, H, W, 3]
        albedo = torch.where(rast[..., 3:] > 0, albedo, torch.tensor(0).to(albedo.device)) # remove background

        # get vn and render normal
        vn = self.mesh.vn
        
        normal, _ = dr.interpolate(vn.unsqueeze(0).contiguous(), rast, self.mesh.fn)
        normal = safe_normalize(normal[0])

        # get positions
        xyzs, _ = dr.interpolate(v.unsqueeze(0).contiguous(), rast, self.mesh.f)

        # antialias
        albedo = dr.antialias(albedo, rast, v_clip, self.mesh.f).squeeze(0).clamp(0, 1) # [H, W, 3]
        alpha = dr.antialias(alpha, rast, v_clip, self.mesh.f).squeeze(0).clamp(0, 1) # [H, W, 3]
        depth = rast[0, :, :, [2]] # [H, W]
        
        albedo = albedo + (1 - alpha) * bg_color

        # ssaa
        if ssaa != 1:
            albedo = scale_img_hwc(albedo, (h0, w0))
            alpha = scale_img_hwc(alpha, (h0, w0))
            depth = scale_img_hwc(depth, (h0, w0))
            normal = scale_img_hwc(normal, (h0, w0))
            xyzs = scale_img_hwc(xyzs, (h0, w0))
            texc = scale_img_hwc(texc, (h0, w0))

        results['image'] = albedo
        results['alpha'] = alpha
        results['depth'] = depth
        results['xyzs'] = xyzs
        results['normal'] = (normal + 1) / 2
        results['uvs'] = texc

        return results