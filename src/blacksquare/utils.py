from typing import Any

import numpy as np


def sum_by_group(groups: np.ndarray, values: np.ndarray) -> dict:
    """A utility function to perform grouped summing of values over numpy arrays.

    Args:
        groups: An array describing what group each value corresponds to.
        values: An array of values.

    Returns:
        A dictionary mapping each unique group the sum of its values.
    """
    sort_indices = np.argsort(groups)
    sorted_groups = groups[sort_indices]
    sorted_values = values[sort_indices]
    unique_groups, unique_indices = np.unique(sorted_groups, return_index=True)
    group_values = np.split(sorted_values, unique_indices[1:])
    return {g: v.sum() for g, v in zip(unique_groups, group_values)}


def is_intlike(x: Any) -> bool:
    """A helper function for doing type checking on possibly-numpy integers.

    Args:
        x: The input value.

    Returns:
        True if the number is a python integer or a numpy integer.
    """
    return isinstance(x, (int, np.integer))
