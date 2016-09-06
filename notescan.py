#!/usr/bin/env python

import sys
import os
import re
import subprocess
import cv2
from argparse import ArgumentParser
import numpy as np
from PIL import Image
import scipy.cluster.vq

######################################################################

def quantize(image, bits_per_channel=None):
    
    if bits_per_channel is None:
        bits_per_channel = 6
        
    assert image.dtype == np.uint8

    shift = 8-bits_per_channel
    halfbin = 1 << (shift - 1)
    
    return (((image.astype(int) + halfbin) >> shift)
            << shift) + halfbin

######################################################################

def pack_rgb(rgb):

    orig_shape = None

    if isinstance(rgb, np.ndarray):
        assert rgb.shape[-1] == 3
        orig_shape = rgb.shape[:-1]
    else:
        assert len(rgb) == 3
        rgb = np.array(rgb)

    rgb = rgb.astype(int).reshape((-1, 3))
        
    packed = (rgb[:,0] |
              rgb[:,1] << 8 |
              rgb[:,2] << 16)

    if orig_shape is None:
        return packed
    else:
        return packed.reshape(orig_shape)

######################################################################

def unpack_rgb(packed):

    orig_shape = None

    if isinstance(packed, np.ndarray):
        assert(packed.dtype == int)
        orig_shape = packed.shape
        packed = packed.reshape((-1, 1))

    rgb = (packed & 0xff,
           (packed >> 8) & 0xff,
           (packed >> 16) & 0xff)

    if orig_shape is None:
        return rgb
    else:
        return np.hstack(rgb).reshape(orig_shape + (3,))

######################################################################
    
def get_bg_color(image, bits_per_channel=None):

    assert image.shape[-1] == 3
        
    quantized = quantize(image, bits_per_channel).astype(int)
    packed = pack_rgb(quantized)
              
    unique, counts = np.unique(packed, return_counts=True)

    packed_mode = unique[counts.argmax()]

    return unpack_rgb(packed_mode)

######################################################################

def rgb_to_sv(rgb):

    if not isinstance(rgb, np.ndarray):
        rgb = np.array(rgb)

    axis = len(rgb.shape)-1
    Cmax = rgb.max(axis=axis).astype(np.float32)
    Cmin = rgb.min(axis=axis).astype(np.float32)
    delta = Cmax - Cmin

    S = delta.astype(np.float32) / Cmax.astype(np.float32)
    S = np.where(Cmax == 0, 0, S)
                 
    V = Cmax/255.0

    return S, V

######################################################################

def nearest(pixels, centers):

    pixels = pixels.astype(int)
    centers = centers.astype(int)
    
    n = pixels.shape[0]
    m = pixels.shape[1]
    k = centers.shape[0]
    assert(centers.shape[1] == m)
    
    dists = np.empty((n, k), dtype=pixels.dtype)
    for i in range(k):
        dists[:, i] = ((pixels - centers[i].reshape((1,m)))**2).sum(axis=1)

    return dists.argmin(axis=1)

######################################################################            

def encode(bg_color, fg_pixels, options):

    num_pixels = fg_pixels.shape[0]
    num_train = int(round(num_pixels*options.quantize_fraction))
    
    idx = np.arange(num_pixels)
    np.random.shuffle(idx)
    train = fg_pixels[idx[:num_train]].astype(np.float32)

    centers, _  = scipy.cluster.vq.kmeans(train,
                                          options.num_colors-1,
                                          iter=40)

    labels = nearest(fg_pixels, centers)

    palette = np.vstack((bg_color, centers)).astype(np.uint8)

    return labels+1, palette

######################################################################

def smoosh(pil_img, output_filename, options):

    if pil_img.info.has_key('dpi'):
        dpi_x, dpi_y = pil_img.info['dpi']
    else:
        dpi_x, dpi_y = 300, 300

    if pil_img.mode != 'RGB':
        pil_img = pil_img.convert('RGB')

    img = np.array(pil_img)

    ds_factor = 4

    downsampled = img[::ds_factor, ::ds_factor]

    bg_color = get_bg_color(downsampled, 6)
    print '  got background color', bg_color

    bgS, bgV = rgb_to_sv(bg_color)
    
    imgS, imgV = rgb_to_sv(img)

    S_diff = np.abs(imgS - bgS)
    V_diff = np.abs(imgV - bgV)

    bg = ((V_diff < options.value_threshold) &
          (S_diff < options.sat_threshold))

    print '  quantizing...'


    labels, palette = encode(bg_color, img[~bg], options)

    biglabels = np.zeros(img.shape[:2], dtype=np.uint8)
    biglabels[~bg] = labels.flatten()

    if options.saturate:
        palette = palette.astype(np.float32)
        pmin = palette.min()
        pmax = palette.max()
        palette = 255 * (palette - pmin)/(pmax-pmin)

    if options.white_bg:
        palette[0] = (255,255,255)
        
    palette = palette.astype(np.uint8)

    output_img = Image.fromarray(biglabels, 'P')
    output_img.putpalette(palette.flatten())
    
    output_img.save(output_filename, dpi=(dpi_x, dpi_y))

    print '  wrote', output_filename

######################################################################

def percent(string):
    return float(string)/100.0

######################################################################    

def notescan_main():
    
    parser = ArgumentParser(
        description='convert scanned, hand-written notes to PDF')

    show_default = ' (default %(default)s)'
    
    parser.add_argument('filenames', metavar='IMAGE', nargs='+',
                        help='files to convert')

    parser.add_argument('-b', dest='basename', metavar='BASENAME',
                        default='output_page_',
                        help='output PNG filename base' + show_default)

    parser.add_argument('-p', dest='pdfname', metavar='PDF',
                        default='output.pdf',
                        help='output PDF filename' + show_default)

    parser.add_argument('-v', dest='value_threshold', metavar='PERCENT',
                        type=percent, default='25',
                        help='background value threshold %%'+show_default)

    parser.add_argument('-s', dest='sat_threshold', metavar='PERCENT',
                        type=percent, default='20',
                        help='background saturation '
                        'threshold %%'+show_default)

    parser.add_argument('-n', dest='num_colors', type=int,
                        default='8',
                        help='number of output colors '+show_default)

    parser.add_argument('-q', dest='quantize_fraction',
                        metavar='PERCENT',
                        type=percent, default='5',
                        help='%% of pixels to keep for '
                        'quantizing' + show_default)

    parser.add_argument('-C', dest='crush', action='store_false',
                        default=True, help='do not run pngcrush')

    parser.add_argument('-S', dest='saturate', action='store_false',
                        default=True, help='do not saturate colors')

    parser.add_argument('-w', dest='white_bg', action='store_true',
                        default=False, help='make background white')
    
    options = parser.parse_args()

    filenames = []

    for filename in options.filenames:
        basename = os.path.basename(filename)
        root, _ = os.path.splitext(basename)
        m = re.findall(r'[0-9]+', root)
        if m:
            num = int(m[-1])
        else:
            num = -1
        filenames.append((num, filename))

    filenames.sort()
    filenames = [fn for (_, fn) in filenames]

    page_count = 0

    outputs = []

    have_pngcrush = (options.crush and
                     subprocess.call(['pngcrush', '-q']) == 0)
    
    for input_filename in filenames:

        try:
            pil_img = Image.open(input_filename)
        except IOError:
            print 'warning: error opening ' + input_filename
            continue
            
        output_basename = '{}{:04d}'.format(options.basename, page_count)
        output_filename = output_basename + '.png'
        crush_filename = output_basename + '_crush.png'

        print 'opened', input_filename

        smoosh(pil_img, output_filename, options)

        if have_pngcrush:
            result = subprocess.call(['pngcrush', '-q',
                                      output_filename,
                                      crush_filename])
        else:
            result = -1
            
        if result == 0:
            outputs.append(crush_filename)
            before = os.stat(output_filename)
            after = os.stat(crush_filename)
            fraction = 100.0*(1-float(after.st_size)/before.st_size)
            print '  pngcrush -> {} ({:.1f}% reduction)'.format(
                crush_filename, fraction)
        else:
            outputs.append(output_filename)
            if have_pngcrush:
                print '  warning: pngcrush failed'
                have_pngcrush = False


        print
        page_count += 1

    
    pargs = ['convert'] + outputs + [options.pdfname]
    
    if subprocess.call(pargs) == 0:
        print 'wrote', options.pdfname
    

if __name__ == '__main__':
    
    notescan_main()