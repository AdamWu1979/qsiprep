#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
ITK files handling
~~~~~~~~~~~~~~~~~~


"""
import os
import os.path as op
import subprocess
from mimetypes import guess_type
from tempfile import TemporaryDirectory
import numpy as np
import nibabel as nb

from nipype import logging
from nipype.utils.filemanip import fname_presuffix
from nipype.interfaces.base import (
    traits, TraitedSpec, BaseInterfaceInputSpec, File, InputMultiPath, OutputMultiPath,
    OutputMultiObject, SimpleInterface)
from nipype.interfaces.ants.resampling import ApplyTransformsInputSpec
from fmriprep.interfaces.itk import MCFLIRT2ITK, FUGUEvsm2ANTSwarp
LOGGER = logging.getLogger('nipype.interface')


class DisassembleTransformInputSpec(BaseInterfaceInputSpec):
    in_file = File(exists=True, mandatory=True, desc='ANTs composite transform (h5)')


class DisassembleTransformOutputSpec(TraitedSpec):
    out_transforms = OutputMultiObject(File(exists=True))


class DisassembleTransform(SimpleInterface):
    """Sloppy interface to split h5 transforms to a warp and an affine."""

    input_spec = DisassembleTransformInputSpec
    output_spec = DisassembleTransformOutputSpec

    def _run_interface(self, runtime):
        cmd = ['CompositeTransformUtil', '--disassemble', self.inputs.in_file, 'disassemble']
        affine_out = runtime.cwd + "/00_disassemble_AffineTransform.mat"
        warp_out = runtime.cwd + "/01_disassemble_DisplacementFieldTransform.nii.gz"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        LOGGER.info(" ".join(cmd))
        out, err = proc.communicate()
        if False in (op.exists(affine_out), op.exists(warp_out)):
            raise Exception("unable to unpack composite transform")
        self._results['out_transforms'] = [affine_out, warp_out]
        return runtime


class MultiApplyTransformsInputSpec(ApplyTransformsInputSpec):
    input_image = InputMultiPath(File(exists=True), mandatory=True,
                                 desc='input time-series as a list of volumes after splitting'
                                      ' through the fourth dimension')
    num_threads = traits.Int(1, usedefault=True, nohash=True,
                             desc='number of parallel processes')
    save_cmd = traits.Bool(True, usedefault=True,
                           desc='write a log of command lines that were applied')
    copy_dtype = traits.Bool(False, usedefault=True,
                             desc='copy dtype from inputs to outputs')


class MultiApplyTransformsOutputSpec(TraitedSpec):
    out_files = OutputMultiPath(File(), desc='the output ITKTransform file')
    log_cmdline = File(desc='a list of command lines used to apply transforms')


class MultiApplyTransforms(SimpleInterface):

    """
    Apply the corresponding list of input transforms
    """
    input_spec = MultiApplyTransformsInputSpec
    output_spec = MultiApplyTransformsOutputSpec

    def _run_interface(self, runtime):
        # Get all inputs from the ApplyTransforms object
        ifargs = self.inputs.get()

        # Extract number of input images and transforms
        in_files = ifargs.pop('input_image')
        num_files = len(in_files)
        transforms = ifargs.pop('transforms')
        # Get number of parallel jobs
        num_threads = ifargs.pop('num_threads')
        save_cmd = ifargs.pop('save_cmd')

        # Remove certain keys
        for key in ['environ', 'ignore_exception',
                    'terminal_output', 'output_image']:
            ifargs.pop(key, None)

        # Get a temp folder ready
        tmp_folder = TemporaryDirectory(prefix='tmp-', dir=runtime.cwd)

        # In qsiprep the transforms have already been merged
        xfms_list = transforms
        assert len(xfms_list) == num_files

        # Inputs are ready to run in parallel
        if num_threads < 1:
            num_threads = None

        if num_threads == 1:
            out_files = [_applytfms((
                in_file, in_xfm, ifargs, i, runtime.cwd))
                for i, (in_file, in_xfm) in enumerate(zip(in_files, xfms_list))
            ]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=num_threads) as pool:
                out_files = list(pool.map(_applytfms, [
                    (in_file, in_xfm, ifargs, i, runtime.cwd)
                    for i, (in_file, in_xfm) in enumerate(zip(in_files, xfms_list))]
                ))
        tmp_folder.cleanup()

        # Collect output file names, after sorting by index
        self._results['out_files'] = [el[0] for el in out_files]

        if save_cmd:
            self._results['log_cmdline'] = os.path.join(runtime.cwd, 'command.txt')
            with open(self._results['log_cmdline'], 'w') as cmdfile:
                print('\n-------\n'.join([el[1] for el in out_files]),
                      file=cmdfile)
        return runtime


def _applytfms(args):
    """
    Applies ANTs' antsApplyTransforms to the input image.
    All inputs are zipped in one tuple to make it digestible by
    multiprocessing's map
    """
    import nibabel as nb
    from nipype.utils.filemanip import fname_presuffix
    from ..niworkflows.interfaces.fixes import FixHeaderApplyTransforms as ApplyTransforms

    in_file, in_xform, ifargs, index, newpath = args
    out_file = fname_presuffix(in_file, suffix='_xform-%05d' % index,
                               newpath=newpath, use_ext=True)

    copy_dtype = ifargs.pop('copy_dtype', False)
    xfm = ApplyTransforms(
        input_image=in_file, transforms=in_xform, output_image=out_file, **ifargs)
    xfm.terminal_output = 'allatonce'
    xfm.resource_monitor = False
    runtime = xfm.run().runtime

    if copy_dtype:
        nii = nb.load(out_file)
        in_dtype = nb.load(in_file).get_data_dtype()

        # Overwrite only iff dtypes don't match
        if in_dtype != nii.get_data_dtype():
            nii.set_data_dtype(in_dtype)
            nii.to_filename(out_file)

    return (out_file, runtime.cmdline)


def _arrange_xfms(transforms, num_files, tmp_folder):
    """
    Convenience method to arrange the list of transforms that should be applied
    to each input file. Not needed in qsiprep
    """
    base_xform = ['#Insight Transform File V1.0', '#Transform 0']
    # Initialize the transforms matrix
    xfms_T = []
    for i, tf_file in enumerate(transforms):
        # If it is a deformation field, copy to the tfs_matrix directly
        if guess_type(tf_file)[0] != 'text/plain':
            xfms_T.append([tf_file] * num_files)
            continue

        with open(tf_file) as tf_fh:
            tfdata = tf_fh.read().strip()

        # If it is not an ITK transform file, copy to the tfs_matrix directly
        if not tfdata.startswith('#Insight Transform File'):
            xfms_T.append([tf_file] * num_files)
            continue

        # Count number of transforms in ITK transform file
        nxforms = tfdata.count('#Transform')

        # Remove first line
        tfdata = tfdata.split('\n')[1:]

        # If it is a ITK transform file with only 1 xform, copy to the tfs_matrix directly
        if nxforms == 1:
            xfms_T.append([tf_file] * num_files)
            continue

        if nxforms != num_files:
            raise RuntimeError('Number of transforms (%d) found in the ITK file does not match'
                               ' the number of input image files (%d).' % (nxforms, num_files))

        # At this point splitting transforms will be necessary, generate a base name
        out_base = fname_presuffix(tf_file, suffix='_pos-%03d_xfm-{:05d}' % i,
                                   newpath=tmp_folder.name).format
        # Split combined ITK transforms file
        split_xfms = []
        for xform_i in range(nxforms):
            # Find start token to extract
            startidx = tfdata.index('#Transform %d' % xform_i)
            next_xform = base_xform + tfdata[startidx + 1:startidx + 4] + ['']
            xfm_file = out_base(xform_i)
            with open(xfm_file, 'w') as out_xfm:
                out_xfm.write('\n'.join(next_xform))
            split_xfms.append(xfm_file)
        xfms_T.append(split_xfms)

    # Transpose back (only Python 3)
    return list(map(list, zip(*xfms_T)))


def compose_affines(reference_image, affine_list, output_file):
    """Use antsApplyTransforms to get a single affine from multiple affines."""
    cmd = "antsApplyTransforms -d 3 -r %s -o Linear[%s, 1] " % (
        reference_image, output_file)
    cmd += " ".join(["--transform %s" % trf for trf in affine_list])
    os.system(cmd)
    assert os.path.exists(output_file)
    return output_file


def rotation_matrix_from_transform(transform):
    """Get the rotation matrix from an itk transform."""
    cmd = "antsTransformInfo " + transform
    proc = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    result = str(out)
    if not len(result):
        raise ValueError("%s returned no transform info" % transform)
    lines = [line.strip() for line in result.split("\\n")]
    start_lines = [linenum for linenum, contents in enumerate(lines) if contents == "Matrix:"]
    if not len(start_lines):
        raise ValueError("Unable to read rotation matrix from " + transform)
    if len(start_lines) > 1:
        raise ValueError("Too many rotation matrices in " + transform)
    start_line = start_lines[0]
    matrix_lines = lines[(start_line+1):(start_line+4)]
    return np.array([list(map(float, line.split())) for line in matrix_lines])
