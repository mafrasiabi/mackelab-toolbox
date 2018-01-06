# -*- coding: utf-8 -*-
"""
Provides the 'expand_params' method for expanding a parameter description strings.
This allows to use one descriptor to specify multiple parameter sets.



Created on Wed Sep 20 13:19:53 2017

@author: alex
"""

from collections import deque, OrderedDict, namedtuple, Iterable
from types import SimpleNamespace
import hashlib
from numbers import Number
import numpy as np
import scipy as sp
import logging
logger = logging.getLogger(__file__)

from parameters import ParameterSet

from . import iotools
from .utils import flatten

##########################
# Transformed parameters
##########################

import simpleeval
import ast
import operator

class Transform:
    # Replace the "safe" operators with their standard forms
    # (simpleeval implements safe_add, safe_mult, safe_exp, which test their
    #  input but this does not work with non-numerical types.)
    _operators = simpleeval.DEFAULT_OPERATORS
    _operators.update(
        {ast.Add: operator.add,
         ast.Mult: operator.mul,
         ast.Pow: operator.pow})
    # Allow evaluation to find operations in standard namespaces
    namespaces = {'np': np,
                  'sp': sp}

    def __new__(cls, *args, **kwargs):
        # Make calling Transform on a Transform instance just return the instance
        if len(args) > 0 and isinstance(args[0], Transform):
            # Don't return a new instance; just return this one
            return args[0]
        else:
            return super().__new__(cls)

    def __init__(self, transform_desc):
        # No matter what we do in __new__, __init__ is called, so we need
        # to check this again.
        if not isinstance(transform_desc, Transform):
            xname, expr = transform_desc.split('->')
            self.xname = xname.strip()
            self.expr = expr.strip()


    def __call__(self, x):
        names = {self.xname: x}
        names.update(self.namespaces)
        try:
            res = simpleeval.simple_eval(
                self.expr,
                operators=Transform._operators,
                names=names)
        except simpleeval.NameNotDefined as e:
            e.args = ((e.args[0] +
                       "\n\nThis may be due to a module function in the transform "
                       "expression (only numpy and scipy, as 'np' and 'sp', are "
                       "available by default).\nIf '{}' is a module or class, you can "
                       "make it available by adding it to the transform namespace: "
                       "`Transform.namespaces.update({{'{}': {}}})`.\nSuch a line would "
                       "typically be included at the beginning of the execution script "
                       "(it does not need to be in the same module as the one where "
                       "the transform is defined, as long as it is executed before)."
                       .format(e.name, e.name, e.name),)
                      + e.args[1:])
            raise
        return res

    @property
    def desc(self):
        return self.xname + " -> " + self.expr

class TransformedVar:
    def __init__(self, desc, *args, orig=None, new=None):
        """
        Should pass exactly one of the parameters `orig` and `new`
        TODO: Allow non-symbolic variables. Possible implementation:
            Test to see if orig/new is a constant, callable or symbolic.
            Set the other orig/new to the same type.
        """
        if len(args) > 0:
            raise TypeError("TransformedVar() takes only one positional argument.")
        if not( (orig is None) != (new is None) ):  #xor
            raise ValueError("Exactly one of `orig`, `new` must be specified.")
        self.to = Transform(desc.to)
        self.back = Transform(desc.back)
        if orig is not None:
            #assert(shim.issymbolic(orig))
            self.orig = orig
            self.new = self.to(self.orig)
        elif new is not None:
            #assert(shim.issymbolic(new))
            self.new = new
            self.orig = self.back(new)
        names = [nm.strip() for nm in desc.name.split('->')]
        assert(len(names) == 2)
        if self.orig.name is None:
            self.orig.name = names[0]
        else:
            assert(self.orig.name == names[0])
        if self.new.name is None:
            self.new.name = names[1]
        else:
            assert(self.new.name == names[1])

class NonTransformedVar:
    """Provides an interface consistent with TransformedVar."""
    def __init__(self, orig):
        self.orig = orig
        self.to = lambda x: x
        self.back = lambda x: x
        self.new = orig

###########################
# Making file names from parameters
###########################

# We use the string representation of arrays to compute the hash,
# so we need to make sure it's standardized. The values below
# are the NumPy defaults.
_filename_printoptions = {
    'precision': 8,
    'edgeitems': 3,
    'formatter': None,
    'infstr': 'inf',
    'linewidth': 75,
    'nanstr': 'nan',
    'suppress': False,
    'threshold': 1000}

def get_filename(params, suffix=None):
    """
    Generate a unique filename by hashing a parameter file.
    """
    if params == '':
        basename = ""
    else:
        # Standardize the numpy print options, which affect output from str()
        stored_printoptions = np.get_printoptions()
        np.set_printoptions(**_filename_printoptions)
        # We need a sorted dictionary of parameters, so that the hash is consistent
        flat_params = params_to_arrays(params).flatten()
            # flatten avoids need to sort recursively
            # _params_to_arrays normalizes the data
        sorted_params = OrderedDict( (key, flat_params[key]) for key in sorted(flat_params) )
        basename = hashlib.sha1(bytes(repr(sorted_params), 'utf-8')).hexdigest()
        basename += '_'
        # Reset the saved print options
        np.set_printoptions(**stored_printoptions)
    if isinstance(suffix, str):
        suffix = suffix.lstrip('_')
    if suffix is None or suffix == "":
        assert(len(basename) > 1 and basename[-1] == '_')
        return basename[:-1] # Remove underscore
    elif isinstance(suffix, str):
        return basename + suffix
    elif isinstance(suffix, Iterable):
        assert(len(suffix) > 0)
        return basename + '_'.join([str(s) for s in suffix])
    else:
        return basename + str(suffix)

def params_to_arrays(params):
    """
    Recursively apply `np.array()` to all values in a ParameterSet. This allows
    arrays to be specified in files as nested lists, which are more readable.
    Also converts dictionaries to parameter sets.
    """
    for name, val in params.items():
        if isinstance(val, (ParameterSet, dict)):
            params[name] = params_to_arrays(val)
        elif (not isinstance(val, str)
            and isinstance(val, Iterable)
            and all(isinstance(v, Number) for v in flatten(val))):
                # The last condition leaves objects like ('lin', 0, 1) as-is;
                # otherwise they would be casted to a single type
            params[name] = np.array(val)
    return ParameterSet(params)


###########################
# Parameter file expansion
###########################

ExpandResult = namedtuple("ExpandResult", ['strs', 'done'])

def expand_params(param_str, fail_on_unexpanded=False, parser=None):
    """
    Expand a parameter description into multiple descriptions.
    The default parser expands contents of the form "*[a, b, ...]" into multiple
    files with the starred expression replaced by "a", "b", ….

    The default parser expands on '*' and recognizes '()', '[]' and '{}' as
    brackets. This can be changed by explicitly passing a custom parser.
    The easiest way to obtain a custom parser is to instantiate one with
    Parser, and change its 'open_brackets', 'close_brackets', 'separators' and
    'expanders' attributes.

    Parameters
    ----------
    param_str: str
        The string descriptor for the parameters.
    fail_on_unexpanded: bool (default False)
        (Optional) Specify whether to fail when an expansion character is found
        but unable to be expanded. By default such an error is ignored, but
        if your parameter format allows it, consider setting it to True to
        catch formatting errors earlier.
    parser: object
        (Optional) Only required if one wishes to replace the default parser.
        The passed object must provide an 'extract_blocks' method, which itself
        returns a dictionary of Parser.Block elements.

    Returns
    -------
    list of strings
        Each element is a complete string description for oneset of parameters.
    """

    if parser is None:
        parser = Parser()
    param_strs = [strip_comments(param_str)]
    done = False
    while not done:
        res_lst = [_expand(s, fail_on_unexpanded, parser) for s in param_strs]
        assert(isinstance(res.strs, list) for res in res_lst)
        param_strs = [s for res in res_lst for s in res.strs]
        done = all(res.done for res in res_lst)

    return param_strs

def expand_param_file(param_path, output_path,
                      fail_on_unexpanded=False, parser=None,
                      max_files=1000):
    """
    Load the file located at 'param_path' and call expand_params on its contents.
    The resulting files are saved at the location specified by 'output_pathn',
    with a number appended to each's filename to make it unique.

    Parameters
    ----------
    param_path: str
        Path to a parameter file.
    output_path: str
        Path to which the expanded parameter files will be saved. If this is
        'path/to/file.ext', then each will be saved as 'path/to/file_1.ext',
        'path/to/file_2.ext', etc.
    fail_on_unexpanded: bool (default False)
        (Optional) Specify whether to fail when an expansion character is found
        but unable to be expanded. By default such an error is ignored, but
        if your parameter format allows it, consider setting it to True to
        catch formatting errors earlier.
    parser: object
        (Optional) Only required if one wishes to replace the default parser.
        The passed object must provide an 'extract_blocks' method, which itself
        returns a dictionary of Parser.Block elements.
    max_files: int
        (Optional) Passed to iotools.get_free_file. Default is 1000.

    Returns
    -------
    None
    """
    with open(param_path, 'r') as f:
        src_str = f.read()

    pathnames = []
    for ps in expand_params(src_str, fail_on_unexpanded, parser):
        f, pathname = iotools.get_free_file(output_path, bytes=False,
                                            force_suffix=True,
                                            max_files=max_files)
        f.write(ps)
        f.close()
        pathnames.append(pathname)

    #print("Parameter files were written to " + ', '.join(pathnames))
        # TODO: Use logging
    return pathnames

def strip_comments(s):
    return '\n'.join(line.partition('#')[0].rstrip()
                     for line in s.splitlines())

def _expand(s, fail_on_unexpanded, parser):
    #param_strs = [s]  # Start with a single parameter string
    blocks = parser.extract_blocks(s)
    for i, c in enumerate(s):
        if c in parser.expanders:
            if i+1 in blocks:
                block = blocks[i+1]
                expanded_str = [s[:i] + str(el) + s[block.stop:]
                                for el in block.elements.values()]
                # Return list of expanded strings and continuation flag
                return ExpandResult(expanded_str, False)
            elif fail_on_unexpanded:
                raise ValueError("Expansion identifier '*' at position {} "
                                 "must be followed by a bracketed expression.\n"
                                 "Context: '{}'."
                                 .format(i, s[max(i-10,0):i+10]))
    # Found nothing to expand; return the given string and termination flag
    # Wrap the string in a list to match the expected format
    return ExpandResult([s], True)

class Parser():
    """Basic parser for nested structures with opening and closing brackets."""

    # Default values.
    # These can be changed on a per instance by reassigning the attributes
    open_brackets = ['[', '{']
    close_brackets = [']', '}']  # Order must match that of open_brackets
        # Parentheses are not included because "*(…)" tends to appear in
        # mathematical expressions
    separators = [',']
    expanders = ['*']

    def get_closer(self, c):
        idx = self.open_brackets.index(c)
        if idx != -1:
            return self.close_brackets[idx]
    def get_opener(self, c):
        idx = self.close_brackets.index(c)
        if idx != -1:
            return self.open_brackets[idx]

    def extract_blocks(self, s):
        block_stack = deque()  # Unclosed blocks
        blocks = {}       # Closed blocks, keyed by their starting index
        for i, c in enumerate(s):
            if c in self.open_brackets:
                block = Block(start=i, opener=c, closer=self.get_closer(c))
                if len(block_stack) > 0:
                    block_stack[-1].blocks.append(block)
                block_stack.append(block)
            elif c in self.close_brackets:
                block = block_stack[-1]
                if len(block_stack) == 0:
                    raise ValueError("Unmatched closing bracket '{}' at position {}."
                                     .format(c, i))
                if c != block_stack[-1].closer:
                    raise ValueError("Closing bracket '{}' at position {} does not "
                                     "match opening bracket '{}' at position {}."
                                     .format(c, i, block.opener,
                                             block.start))
                block.stop = i+1

                # TODO: make this method of the Elements object or something
                el_start_i = list(block.elements.keys())[-1]
                el_stop_i = i
                block.elements[el_start_i] = s[el_start_i:el_stop_i]

                blocks[block.start] = block_stack.pop()
            elif c in self.separators:
                block = block_stack[-1]

                el_start_i = list(block.elements.keys())[-1]
                el_stop_i = i
                block.elements[el_start_i] = s[el_start_i:el_stop_i]

                block.elements[el_stop_i+1] = None

        if len(block_stack) > 0:
            raise ValueError("Unmatched opening bracket '{}' at position {}."
                             .format(block_stack[-1].opener,
                                     block_stack[-1].start))

        return blocks

            # A dictionary of the elements separated by one of the 'separators'
            # The key is the index of the first character

class Block(SimpleNamespace):
    def __init__(self, start, opener, closer):
        super().__init__()
        self.start = start
        self.stop = None
        self.opener = opener
        self.closer = closer
        self.elements = OrderedDict([(start+1, None)])
        self.blocks = []


###################
# ParameterSet sampler
###################


class ParameterSetSampler:
    """
    This class mainly serves two purposes:
      - Convert a distribution definition into a sampler for that parameter
      - Maintain a cache of the state of the RNG, so that draws
        a) are consistent across runs and code changes
           (only changes to the parameter file itself will change the chosen parameters)
        b) do not affect random draws from outside this module
    NOTE: To achieve its goals, this class effectively maintains its own separate
    random number generator. This means that samples produced within this class may not
    be independent from samples produced outside of it. This shouldn't be a problem if
    e.g. the 'other' samples are those used to induce noise in a simulation. However,
    generating other parameters with a separate RNG should be avoided.

    # TODO: Cast parameters with subpopulations as BroadcastableBlockArray ?
    """
    population_attrs = ['population', 'populations', 'mixture', 'mixtures', 'label', 'labels']
    def __init__(self, dists):
        """
        Parameters
        ----------
        dists: ParameterSet
        """
        # Implementation:
        # In order to always sample the same way, we set an order for parameters.
        # We can then sample them sequentially (i.e. each is sampled once, before
        # any one is sampled twice).
        # At any time, we can save the state of the RNG and reload it later to continue sampling.

        dists = ParameterSet(dists)  # Normalize the input (allows e.g. urls)
        self._iter_idx = None        # Internal index for the iterator
        orig_state = np.random.get_state()   # Store the current RNG state so it can be reset later

        # Get population / mixture labels
        #popstrs = [ attr for attr in [getattr(dists, attr, None) for attr in self.population_strs]
                         #if attr is not None ]
        popattrs = [ attr for attr in self.population_attrs if attr in dists ]
        if len(popattrs) > 1:
            raise ValueError("Multiple populations specifications. Only one of {} is needed."
                             .format(population_strs))
        elif len(popattrs) == 1:
            popnames = dists[popattrs[0]]
        else:
            popnames = None

        # Set seed
        if 'seed' in dists:
            np.random.seed(dists.seed)

        # Get all the variable names and fix their order.
        # If we didn't fix their order here, changing the order in the parameter file
        # would change the sampled numbers.
        self.varnames = sorted([name for name in dists if name not in ['seed'] + popattrs])

        # Create the samplers
        self._samplers = {
            varname: ParameterSampler(varname, dists[varname], popnames)
            for varname in self.varnames }

        self._samplers[self.varnames[0]].set_previous(
            self._samplers[self.varnames[-1]], -1)
        for i in range(1, len(self.varnames)):
            self._samplers[self.varnames[i]].set_previous(
                self._samplers[self.varnames[i-1]], 0)

        # Reset the RNG to its external state
        self.rng_state = np.random.get_state()
        np.random.set_state(orig_state)

    # At the moment we shouldn't access samplers directly, because they don't
    # set the RNG state. Eventually we should change this, and then providing
    # this iterator might become a good idea
    # #######
    # # Define iterator
    # def __iter__(self):
    #     self._iter_idx = -1  # Indicates index of last returned sampler
    #     return self

    # def __next__(self):
    #     if len(self.varnames) <= self._iter_idx + 1:
    #         self._iter_idx = None
    #         return StopIteration
    #     else:
    #         self._iter_idx += 1
    #         return self._samplers[self.varnames[self._iter_idx]]
    # # End iterator definition
    # #######

    def sample(self, varname=None):
        """
        Return a sample for the variable identified with 'varname'.
        'Varname' can be a list of names, in which case a ParameterSet
        instance is returned, with each entry keyed by a variable name.
        If no variable is specified, the full set is sampled and
        returned as a ParameterSet.
        """
        orig_state = np.random.get_state()
        np.random.set_state(self.rng_state)

        if varname is None:
            varname = self.varnames

        if isinstance(varname, str) or not isinstance(varname, Iterable):
            res = self._samplers[varname]()
        else:
            res = ParameterSet(
                {name: self._samplers[name]() for name in varname})

        self.rng_state = np.random.get_state()
        np.random.set_state(orig_state)

        return res

class ParameterSampler:
    """
    Implements one of the samplers in ParameterSetSampler.
    Samplers are set as a circular chain: before computing a new sample,
    each checks the previous sampler to see if it has been computed up to
    the same index, plus an offset (offsets should be 0 or negative).
    This is done to ensure that the same parameter set (if it specifies
    a seed) always returns the same draws.

    Sampling happens in the __call__() method.
    """
    # TODO: See if some code can be shared with pymc3.PyMCPrior.get_dist()
    def __init__(self, name, desc, popnames=None):
        if not isinstance(desc, ParameterSet):
            # It's a fixed value: no need for sampling
            self.sampled_idx = None   # This indicates that we aren't sampling
            def get_sample():
                logger.debug("Getting {} sample.".format(self.name))
                return np.array(desc)
        else:
            if 'dist' not in desc:
                raise ValueError("Unrecognized distribution type '{}'."
                                .format(desc.dist))
            if popnames is None:
                # Provide a default population name, in case there is only one
                # population (in which case no name is necessary)
                popnames = ["pop1"]
            self.sampled_idx = 0
            shapes = [()]
            pop_pattern = ()
            for s in desc.shape:
                if not isinstance(s, str):
                    shapes = [ r + (s,) for r in shapes ]
                    pop_pattern += (False,)
                else:
                    pop_pattern += (True,)
                    pop_sizes = s.split('+')
                    if len(pop_sizes) != len(popnames):
                        raise ValueError("The parameter '{}' has a shape with {} "
                                         "components, but we have {} populations."
                                         .format(name, len(pop_sizes), len(popnames)))
                    shapes = [ r + (int(psize),)
                               for r in shapes
                               for psize in pop_sizes ]

            pop_samplers = type(self).PopSampler(desc)

            def key(*poplabels):
                return ','.join(poplabels)
            n = len(popnames)

            # TODO: Remove special cases/pop_pattern and make a generic
            #       function that works with any shape
            if pop_pattern == (True,):
                def get_sample():
                    logger.debug("Getting {} sample.".format(self.name))
                    return np.block(
                        [pop_samplers[key(pop)](shape)
                         for pop, shape in zip(popnames, shapes)])
            elif pop_pattern == (False, True):
                def get_sample():
                    logger.debug("Getting {} sample.".format(self.name))
                    return np.block(
                        [ [ pop_samplers[key(pop)](shape)
                            for pop, shape in zip(popnames, shapes)] ] )
            elif pop_pattern == (True, False):
                def get_sample():
                    logger.debug("Getting {} sample.".format(self.name))
                    return np.block(
                        [ [pop_samplers[key(pop)](shape)]
                          for pop, shape in zip(popnames, shapes) ] )
            elif pop_pattern == (True, True):
                def get_sample():
                    logger.debug("Getting {} sample.".format(self.name))
                    return np.block(
                        [ [ pop_samplers[key(pop1, pop2)](shapes[i + j])
                            for pop2, j in zip(popnames, range(0, n**1, n**0)) ]
                          for pop1, i in zip(popnames, range(0, n**2, n**1)) ] )

        if isinstance(desc, ParameterSet) and 'transform' in desc:
            inverse = Transform(desc.transform.back)
            self._get_sample = lambda : inverse(get_sample())
        else:
            self._get_sample = get_sample
        self._cache = deque()
        self.name = name # Not actually used, but useful e.g. for debugging

    # =======
    class PopSampler:
        """Retrieval interface for the different block samplers in ParameterSampler"""
        def __init__(self, distparams):
            self.distparams = distparams
            self.key = None

        def __getitem__(self, key):
            self.key = key    # Used in __getattr__
            if self.dist == 'normal':
                def sample_pop(size):
                    self.key = key    # Used in __getattr__
                    res = np.random.normal(self.loc,
                                           self.scale, size=size)
                    self.key = None
                    return res
            else:
                raise ValueError("Unrecognized distribution type '{}'."
                                .format(distparams.dist))
            self.key = None
            return sample_pop

        # Retrieve the population-specific
        # parameter, or fall back to the global one if the first
        # isn't given
        def __getattr__(self, attr):
            if attr in self.distparams[self.key]:
                return getattr(self.distparams[self.key], attr)
            else:
                return getattr(self.distparams, attr)
    # =======

    def __call__(self):
        if len(self._cache) == 0:
            self._sample()
        return self._cache.popleft()

    def _sample(self, sample_i=None):
        if self.sampled_idx is None:
            self._cache.append(self._get_sample())
        else:
            if sample_i is None:
                sample_i = self.sampled_idx + 1
            if sample_i > self.sampled_idx:
                while self.previous.sampled_idx < sample_i + self.previous_offset:
                    self.previous._sample(sample_i + self.previous_offset)
                self.sampled_idx += 1
                self._cache.append(self._get_sample())
            else:
                pass
                #assert(len(self._cache) > 0)

    @property
    def previous(self):
        if self._previous.sampled_idx is None:
            return self._previous.previous
        else:
            return self._previous

    @property
    def previous_offset(self):
        if self._previous.sampled_idx is None:
            # Add the previous sampler's offset, since it's skipped over
            return self._previous_offset + self._previous.previous_offset
        else:
            return self._previous_offset

    def set_previous(self, previous_sampler, offset):
        """Set the previous ParameterSampler in the chain."""
        if offset > 0:
            raise ValueError("Offset cannot be positive.")
        if offset not in [0, -1]:
            logger.warning("ParameterSampler index offsets are usually either 0 or -1. "
                           "You specified {}.".format(offset))
        self._previous = previous_sampler
        self._previous_offset = offset
