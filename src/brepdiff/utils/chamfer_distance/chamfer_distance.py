import torch
import os

from torch.utils.cpp_extension import load

current_dir = os.path.dirname(os.path.abspath(__file__))

cd = load(
    name="cd",
    sources=[
        os.path.join(current_dir, "chamfer_distance.cpp"),
        os.path.join(current_dir, "chamfer_distance.cu"),
    ],
    build_directory="./",
)


class ChamferDistanceFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, xyz1, xyz2):
        device = xyz1.device

        # 2 dimensional
        if xyz1.shape[2] == 2:
            xyz1 = torch.cat([xyz1, torch.zeros(1, xyz1.shape[1], 1).to(device)], dim=2)
            xyz2 = torch.cat([xyz2, torch.zeros(1, xyz2.shape[1], 1).to(device)], dim=2)

        batchsize, n, _ = xyz1.size()
        _, m, _ = xyz2.size()
        xyz1 = xyz1.contiguous()
        xyz2 = xyz2.contiguous()
        dist1 = torch.zeros(batchsize, n)
        dist2 = torch.zeros(batchsize, m)

        idx1 = torch.zeros(batchsize, n, dtype=torch.int)
        idx2 = torch.zeros(batchsize, m, dtype=torch.int)

        if not xyz1.is_cuda:
            cd.forward(xyz1, xyz2, dist1, dist2, idx1, idx2)
        else:
            dist1 = dist1.to(device)
            dist2 = dist2.to(device)
            idx1 = idx1.to(device)
            idx2 = idx2.to(device)
            cd.forward_cuda(xyz1, xyz2, dist1, dist2, idx1, idx2)

        ctx.save_for_backward(xyz1, xyz2, idx1, idx2)
        return dist1, dist2, idx1, idx2

    @staticmethod
    def backward(ctx, graddist1, graddist2, gradidx1, gradidx2):
        xyz1, xyz2, idx1, idx2 = ctx.saved_tensors

        graddist1 = graddist1.contiguous()
        graddist2 = graddist2.contiguous()

        gradxyz1 = torch.zeros(xyz1.size())
        gradxyz2 = torch.zeros(xyz2.size())

        if not graddist1.is_cuda:
            cd.backward(
                xyz1, xyz2, gradxyz1, gradxyz2, graddist1, graddist2, idx1, idx2
            )
        else:
            gradxyz1 = gradxyz1.to(xyz1.device)
            gradxyz2 = gradxyz2.to(xyz1.device)
            cd.backward_cuda(
                xyz1, xyz2, gradxyz1, gradxyz2, graddist1, graddist2, idx1, idx2
            )

        return gradxyz1, gradxyz2


class ChamferDistance(torch.nn.Module):
    def forward(self, xyz1, xyz2):
        return ChamferDistanceFunction.apply(xyz1, xyz2)
