import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

def gen_filters(size_x: int, size_y: int, dct_or_idct_fun: callable) -> np.ndarray:
    tile_size_x = 8
    filters = np.zeros((size_x * size_y, size_x, size_y))
    for k_y in range(size_y):
        for k_x in range(size_x):
            for n_y in range(size_y):
                for n_x in range(size_x):
                    filters[k_y * tile_size_x + k_x, n_y, n_x] = dct_or_idct_fun(n_y, k_y, size_y) * dct_or_idct_fun(
                        n_x,
                        k_x,
                        size_x)
    return filters


def get_jpeg_yuv_filter_mask(image_shape: tuple, window_size: int, keep_count: int):
    mask = np.zeros((window_size, window_size), dtype=np.uint8)

    index_order = sorted(((x, y) for x in range(window_size) for y in range(window_size)),
                         key=lambda p: (p[0] + p[1], -p[1] if (p[0] + p[1]) % 2 else p[1]))

    for i, j in index_order[0:keep_count]:
        mask[i, j] = 1

    return np.tile(mask, (int(np.ceil(image_shape[0] / window_size)),
                          int(np.ceil(image_shape[1] / window_size))))[0: image_shape[0], 0: image_shape[1]]


def dct_coeff(n, k, N):
    return np.cos(np.pi / N * (n + 1. / 2.) * k)


def idct_coeff(n, k, N):
    return (int(0 == n) * (- 1 / 2) + np.cos(
        np.pi / N * (k + 1. / 2.) * n)) * np.sqrt(1 / (2. * N))


def rgb2yuv(image_rgb, image_yuv_out):
    """ Transform the image from rgb to yuv """
    image_yuv_out[:, 0, :, :] = 0.299 * image_rgb[:, 0, :, :].clone() + 0.587 * image_rgb[:, 1, :, :].clone() + 0.114 * image_rgb[:, 2, :, :].clone() + 0.000035
    image_yuv_out[:, 1, :, :] = -0.14713 * image_rgb[:, 0, :, :].clone() + -0.28886 * image_rgb[:, 1, :, :].clone() + 0.436 * image_rgb[:, 2, :, :].clone() + 0.004179
    image_yuv_out[:, 2, :, :] = 0.615 * image_rgb[:, 0, :, :].clone() + -0.51499 * image_rgb[:, 1, :, :].clone() + -0.10001 * image_rgb[:, 2, :, :].clone() + 0.003960


def rgb2yuv_post(images):
    images[:, 0, :, :] = images[:, 0, :, :] - 0.000035
    images[:, 1, :, :] = images[:, 1, :, :] - 0.004179
    images[:, 2, :, :] = images[:, 2, :, :] - 0.003960
    return images


def yuv2rgb(image_yuv, image_rgb_out):
    """ Transform the image from yuv to rgb """
    image_rgb_out[:, 0, :, :] = image_yuv[:, 0, :, :].clone() + 1.13983 * image_yuv[:, 2, :, :].clone()
    image_rgb_out[:, 1, :, :] = image_yuv[:, 0, :, :].clone() + -0.39465 * image_yuv[:, 1, :, :].clone() + -0.58060 * image_yuv[:, 2, :, :].clone()
    image_rgb_out[:, 2, :, :] = image_yuv[:, 0, :, :].clone() + 2.03211 * image_yuv[:, 1, :, :].clone()


def yuv2rgb2(x):
    """ Transform the image from yuv to rgb """
    x[:, 1, :, :] = x[:, 1, :, :]
    x[:, 2, :, :] = x[:, 2, :, :]
    return x


class JpegCompression(nn.Module):
    def __init__(self, device, yuv_keep_weights=(25, 9, 9)):
        super(JpegCompression, self).__init__()
        self.device = device

        self.dct_conv_weights = torch.tensor(gen_filters(8, 8, dct_coeff), dtype=torch.float32).to(self.device)
        self.dct_conv_weights.unsqueeze_(1)
        self.idct_conv_weights = torch.tensor(gen_filters(8, 8, idct_coeff), dtype=torch.float32).to(self.device)
        self.idct_conv_weights.unsqueeze_(1)

        self.yuv_keep_weighs = yuv_keep_weights
        self.keep_coeff_masks = []

        self.jpeg_mask = None

        # create a new large mask which we can use by slicing for images which are smaller
        self.create_mask((1000, 1000))

    def create_mask(self, requested_shape):
        if self.jpeg_mask is None or requested_shape > self.jpeg_mask.shape[1:]:
            self.jpeg_mask = torch.empty((3,) + requested_shape, device=self.device)
            for channel, weights_to_keep in enumerate(self.yuv_keep_weighs):
                mask = torch.from_numpy(get_jpeg_yuv_filter_mask(requested_shape, 8, weights_to_keep))
                self.jpeg_mask[channel] = mask

    def get_mask(self, image_shape):
        if self.jpeg_mask.shape < image_shape:
            self.create_mask(image_shape)
        # return the correct slice of it
        return self.jpeg_mask[:, :image_shape[1], :image_shape[2]].clone()

    def apply_conv(self, image, filter_type: str):

        if filter_type == 'dct':
            filters = self.dct_conv_weights
        elif filter_type == 'idct':
            filters = self.idct_conv_weights
        else:
            raise ('Unknown filter_type value.')

        image_conv_channels = []
        for channel in range(image.shape[1]):
            image_yuv_ch = image[:, channel, :, :].unsqueeze_(1)
            image_conv = F.conv2d(image_yuv_ch, filters, stride=8)
            image_conv = image_conv.permute(0, 2, 3, 1)
            image_conv = image_conv.view(image_conv.shape[0], image_conv.shape[1], image_conv.shape[2], 8, 8)
            image_conv = image_conv.permute(0, 1, 3, 2, 4)
            image_conv = image_conv.contiguous().view(image_conv.shape[0],
                                                      image_conv.shape[1] * image_conv.shape[2],
                                                      image_conv.shape[3] * image_conv.shape[4])

            image_conv.unsqueeze_(1)

            # image_conv = F.conv2d()
            image_conv_channels.append(image_conv)

        image_conv_stacked = torch.cat(image_conv_channels, dim=1)

        return image_conv_stacked

    def forward(self, images):

        image_yuv = rgb2yuv_post(images)

        # pad the image so that we can do dct on 8x8 blocks
        pad_height = (8 - image_yuv.shape[2] % 8) % 8
        pad_width = (8 - image_yuv.shape[3] % 8) % 8
        image_yuv = nn.ZeroPad2d((0, pad_width, 0, pad_height))(image_yuv)

        assert image_yuv.shape[2] % 8 == 0
        assert image_yuv.shape[3] % 8 == 0

        # apply dct
        image_dct = self.apply_conv(image_yuv, 'dct')
        # get the jpeg-compression mask
        mask = self.get_mask(image_dct.shape[1:])
        # multiply the dct-ed image with the mask.
        image_dct_mask = torch.mul(image_dct, mask)

        # apply inverse dct (idct)
        image_idct = self.apply_conv(image_dct_mask, 'idct')

        noised_image = image_idct[:, :, :image_idct.shape[2] - pad_height, :image_idct.shape[3] - pad_width].clone()

        return noised_image