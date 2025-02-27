# General reader for 4D-STEM datasets.

import pathlib
from os.path import exists, splitext
from typing import Union, Optional
import h5py

from py4DSTEM.io.native.read import read_py4DSTEM
from py4DSTEM.io.utils import parse_filetype


def read(
    filepath: Union[str,pathlib.Path],
    root: Optional[str] = None,
    tree: Optional[Union[bool,str]] = True,
    **kwargs,
    ):
    """
    File reader for files written by py4DSTEM. To load non-native
    file types, use py4DSTEM.import_file.

    For files written by py4DSTEM v0.13+, the arguments this function
    accepts and their behaviors are below. For older verions, see
    the docstring for `py4DSTEM.io.native.legacy.read_py4DSTEM_legacy`
    for keyword arguments and their behaviors.


    Args:
        filepath (str or Path): the file path
        root (str): the path to the root data group in the HDF5 file
            to read from. To examine an HDF5 file written by py4DSTEM
            in order to determine this path, call
            `py4DSTEM.print_h5_tree(filepath)`. If left unspecified,
            looks in the file and if it finds a single top-level
            object, loads it. If it finds multiple top-level objects,
            prints a warning and returns a list of root paths to the
            top-level object found
        tree (bool or str): indicates what data should be loaded,
            relative to the root group specified above.  must be in
            (`True` or `False` or `noroot`).  If set to `False`, the
            only the data in the root group is loaded, plus any
            associated calibrations.  If set to `True`, loads the root
            group, and all other data groups nested underneath it
            in the file tree.  If set to `'noroot'`, loads all other
            data groups nested under the root group in the file tree,
            but does *not* load the data inside the root group (allowing,
            e.g., loading all the data nested under a DataCube without
            loading the whole datacube).

    Returns:
        (the data)
    """

    # parse filetype
    er1 = f"filepath must be a string or Path, not {type(filepath)}"
    er2 = f"specified filepath '{filepath}' does not exist"
    assert(isinstance(filepath, (str,pathlib.Path) )), er1
    assert(exists(filepath)), er2

    filetype = parse_filetype(filepath)
    assert filetype == "py4DSTEM", "Incompatible file type for py4DSTEM.io.read. To non-native files must be read with py4DSTEM.io.import_file"

    # prepare kwargs
    kwargs['root'] = root
    kwargs['tree'] = tree

    # load data
    data = read_py4DSTEM(
        filepath,
        **kwargs
    )

    return data
