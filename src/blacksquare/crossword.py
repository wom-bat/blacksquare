from __future__ import annotations

import copy
import io
import time
from secrets import token_hex
from typing import Any, Iterable, Iterator

import networkx as nx
import numpy as np
import puz
import rich.box
from rich.console import Console
from rich.live import Live
from rich.table import Table

from blacksquare.cell import Cell
from blacksquare.html import CSS_TEMPLATE
from blacksquare.symmetry import Symmetry
from blacksquare.types import (
    CellIndex,
    CellValue,
    Direction,
    SpecialCellValue,
    WordIndex,
)
from blacksquare.utils import is_intlike
from blacksquare.word import Word
from blacksquare.word_list import DEFAULT_WORDLIST, WordList

try:
    import pypdf
    import weasyprint
except ImportError:
    weasyprint = None
    pypdf = None

BLACK, EMPTY = SpecialCellValue.BLACK, SpecialCellValue.EMPTY
ACROSS, DOWN = Direction.ACROSS, Direction.DOWN


class Crossword:
    """An object representing a crossword puzzle."""

    def __init__(
        self,
        num_rows: int | None = None,
        num_cols: int | None = None,
        grid: list[list[str]] | np.ndarray | None = None,
        symmetry: Symmetry | None = Symmetry.ROTATIONAL,
        word_list: WordList | None = None,
        display_size_px: int = 450,
    ):
        """Creates a new Crossword object.

        Args:
            num_rows: The number of rows in the puzzle. Either this or a grid must be
                provided. If grid is provided, shape will be inferred.
            num_cols: The number of columns in the puzzle. If None, it will either be
                equal to number of rows or inferred from grid.
            grid: A 2-D array of letters from which the grid will be initialized. Can be
                provided instead of num_rows/num_cols.
            word_list: The word list to use by default when finding solutions. If None,
                defaults to the default word list.
            display_size_px: The size in pixels of the largest dimension of the puzzle
                HTML rendering.
        """
        assert (num_rows is not None) ^ (
            grid is not None
        ), "Either specify shape or provide grid."
        if num_rows:
            self._num_rows = num_rows
            if num_cols:
                self._num_cols = num_cols
            else:
                self._num_cols = self._num_rows

            shape = (self._num_rows, self._num_cols)
            cells = [Cell(self, (i, j)) for i, j in np.ndindex(*shape)]
            self._grid = np.array(cells, dtype=object).reshape(shape)
        elif grid is not None:
            assert np.all([len(r) == len(grid[0]) for r in grid])
            self._num_rows = len(grid)
            self._num_cols = len(grid[0])
            shape = (self._num_rows, self._num_cols)
            cells = [Cell(self, (i, j), grid[i][j]) for i, j in np.ndindex(*shape)]
            self._grid = np.array(cells, dtype=object).reshape(shape)

        if (
            symmetry is not None
            and symmetry.requires_square
            and self._num_rows != self._num_cols
        ):
            raise ValueError(f"{symmetry.value} symmetry requires a square grid.")

        self._numbers = np.zeros_like(self._grid, dtype=int)
        self._across = np.zeros_like(self._grid, dtype=int)
        self._down = np.zeros_like(self._grid, dtype=int)
        self._words = {}
        self._dependency_graph: nx.classes.graph.Graph = None
        self._parse_grid()

        self.word_list = word_list if word_list is not None else DEFAULT_WORDLIST
        self.display_size_px = display_size_px
        self.symmetry = symmetry

    def __getitem__(self, key) -> str:
        if isinstance(key, tuple) and len(key) == 2:
            if isinstance(key[0], Direction) and is_intlike(key[1]):
                if key in self._words:
                    return self._words[key]
                else:
                    raise IndexError
            elif is_intlike(key[0]) and is_intlike(key[1]):
                return self._grid[key]
        raise IndexError

    def __setitem__(self, key, value):
        if isinstance(key, tuple) and len(key) == 2:
            if isinstance(key[0], Direction) and is_intlike(key[1]):
                self.set_word(key, value)
            elif is_intlike(key[0]) and is_intlike(key[1]):
                self.set_cell(key, value)
            else:
                raise IndexError
        else:
            raise IndexError

    def __deepcopy__(self, memo):
        copied = copy.copy(self)
        copied._grid = copy.deepcopy(self._grid)
        for cell in copied._grid.ravel():
            cell._parent = copied
        copied._words = copy.deepcopy(self._words)
        for word in copied._words.values():
            word._parent = copied
        copied._dependency_graph = copy.deepcopy(self._dependency_graph)
        return copied

    def __repr__(self):
        longest_filled_word = max(
            self.iterwords(), key=lambda w: len(w) if not w.is_open() else 0
        )
        return f'Crossword("{longest_filled_word.value}")'

    @classmethod
    def from_puz(cls, filename: str) -> Crossword:
        """Creates a Crossword object from a .puz file.

        Args:
            filename: The path of the input .puz file.

        Returns:
            A Crossword object.
        """
        puz_obj = puz.read(filename)
        grid = np.reshape(
            list(puz_obj.solution),
            (puz_obj.height, puz_obj.width),
        )
        xw = cls(grid=grid)
        for cn in puz_obj.clue_numbering().across:
            xw[ACROSS, cn["num"]].clue = cn["clue"]
        for cn in puz_obj.clue_numbering().down:
            xw[DOWN, cn["num"]].clue = cn["clue"]
        return xw

    def to_puz(self, filename: str) -> None:
        """Outputs a .puz file from the Crossword object.

        Args:
            filename: The output path.
        """
        puz_black, puz_empty = ".", "-"
        puz_obj = puz.Puzzle()
        puz_obj.height = self.num_rows
        puz_obj.width = self.num_cols

        char_array = np.array([cell.str for cell in self._grid.ravel()])
        puz_obj.solution = (
            "".join(char_array)
            .replace(EMPTY.str, puz_empty)
            .replace(BLACK.str, puz_black)
        )
        fill_grid = char_array.copy()
        fill_grid[fill_grid != BLACK.str] = puz_empty
        fill_grid[fill_grid == BLACK.str] = puz_black
        puz_obj.fill = "".join(fill_grid)
        sorted_words = sorted(
            list(self.iterwords()), key=lambda w: (w.number, w.direction)
        )
        puz_obj.clues = [w.clue for w in sorted_words]
        puz_obj.cksum_global = puz_obj.global_cksum()
        puz_obj.cksum_hdr = puz_obj.header_cksum()
        puz_obj.cksum_magic = puz_obj.magic_cksum()
        puz_obj.save(filename)

    def to_pdf(
        self,
        filename: str,
        header: list[str] | None = None,
    ) -> None:
        """Outputs a .pdf file in NYT submission format from the Crossword object.

        Args:
            filename: The output path.
            header: A list of strings to put on the output (e.g. name, address, etc.).
                Each list element will be one line in the header.
        """

        if weasyprint is None:
            raise ImportError(
                "Can't import weasyprint, run pip install blacksquare[pdf] to install."
            )

        header_html = "<br />".join(header) if header else ""
        grid_html = f"""
            <html>
            <head><meta charset="utf-8">
            <style>
            @page {{
                margin:0.25 in;
                margin-bottom: 0;
            }}

            @media print {{
            div {{
                break-inside: avoid-page !important;
            }}
            }}


            </style>
            </head>
            <body>
            <div style='font-size:14pt; break-after: avoid-page !important;'>
                {header_html}
            </div>
            <br /> <br /> <br /> <br />
            <div style='margin: auto;'>
                {self._grid_html(size_px=600)}
            </div>
            </body></html>
        """

        row_template = "<tr><td>{}</td><td>{}</td><td>{}</td></tr>"

        def clue_rows(direction):
            row_strings = [
                row_template.format(w.number, w.clue, w.value)
                for w in self.iterwords(direction)
            ]
            return "".join(row_strings)

        clue_html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <style>
                    td {{vertical-align:top;}}
                    table {{
                        text-align:left;
                        width:100%;
                        font-size:16pt;
                        border-spacing:1rem;
                    }}
                </style>
            </head>
            <body>
            <table><tbody>
            <tr><td colspan="3">ACROSS</td></tr>
            {clue_rows(ACROSS)}
            <tr><td></td></tr>
            <tr><td colspan="3">DOWN</td></tr>
            {clue_rows(DOWN)}
            </tbody></table>
            </body></html>
        """
        merger = pypdf.PdfWriter()
        for html_page in [grid_html, clue_html]:
            pdf = weasyprint.HTML(string=html_page, encoding="UTF-8").write_pdf()
            merger.append(pypdf.PdfReader(io.BytesIO(pdf)))
        merger.write(str(filename))
        merger.close()

    @property
    def num_rows(self) -> int:
        """The number of rows in the puzzle"""
        return self._num_rows

    @property
    def num_cols(self) -> int:
        """The number of columns in the puzzle"""
        return self._num_cols

    @property
    def clues(self) -> dict[WordIndex, str]:
        """A dict mapping word index to clue."""
        return {index: w.clue for index, w in self._words.items()}

    def get_symmetric_cell_index(
        self, index: CellIndex, force_list: bool = False
    ) -> CellIndex | list[CellIndex] | None:
        """Gets the index of a symmetric grid cell. Useful for enforcing symmetry.

        Args:
            index: The input cell index.
            force_list: Whether to require that single indices are returned as a list.

        Returns:
            The index (or indices) of the cell symmetric to the input.
        """
        if not self.symmetry:
            return [] if force_list else None
        elif self.symmetry.is_multi_image:
            results = self.symmetry.apply(self._grid)
            return list({r.grid[index].index for r in results})
        else:
            image = self.symmetry.apply(self._grid).grid[index].index
            return [image] if force_list else image

    def get_symmetric_word_index(
        self, word_index: WordIndex, force_list: bool = False
    ) -> WordIndex | list[WordIndex] | None:
        """Gets the index of a symmetric word. Useful for enforcing symmetry.

        Args:
            index: The input word index.
            force_list: Whether to require that single indices are returned as a list.

        Returns:
            The index (or indices) of the word symmetric to the input.
        """
        dir = word_index[0]
        mask = self._get_word_mask(word_index)
        if not self.symmetry:
            return [] if force_list else None
        elif self.symmetry.is_multi_image:
            results = self.symmetry.apply(self._grid)
            new_indices = set()
            for result in results:
                new_dir = dir.opposite if result.word_direction_rotated else dir
                new_indices.add(result.grid[mask][0].get_parent_word(new_dir).index)
            return list(new_indices)
        else:
            result = self.symmetry.apply(self._grid)
            new_dir = dir.opposite if result.word_direction_rotated else dir
            image = result.grid[mask][0].get_parent_word(new_dir).index
            return [image] if force_list else image

    def _parse_grid(self) -> None:
        """Updates all indices to reflect the state of the _grid property."""
        old_across, old_down = self._across, self._down
        padded = np.pad(self._grid, 1, constant_values=Cell(None, (None, None), BLACK))
        shifted_down, shifted_right = padded[:-2, 1:-1], padded[1:-1, :-2]
        shifted_up, shifted_left = padded[2:, 1:-1], padded[1:-1, 2:]
        is_open = ~np.equal(self._grid, BLACK)
        starts_down, starts_across = (
            np.equal(x, BLACK) for x in (shifted_down, shifted_right)
        )
        too_short_down = np.equal(shifted_up, BLACK) & np.equal(shifted_down, BLACK)
        too_short_across = np.equal(shifted_left, BLACK) & np.equal(
            shifted_right, BLACK
        )

        starts_down = starts_down & ~too_short_down
        starts_across = starts_across & ~too_short_across
        needs_num = is_open & (starts_down | starts_across)
        self._numbers = np.reshape(np.cumsum(needs_num), self._grid.shape) * needs_num
        self._across = np.maximum.accumulate(starts_across * self._numbers, axis=1) * (
            is_open & ~too_short_across
        )
        self._down = np.maximum.accumulate(starts_down * self._numbers) * (
            is_open & ~too_short_down
        )

        def get_cells_to_nums(ordered_nums: np.ndarray) -> dict[tuple[int, ...], int]:
            flattened = ordered_nums.ravel()
            word_divs = np.flatnonzero(np.diff(flattened, prepend=-1))
            nums = flattened[word_divs]
            groups = np.split(np.arange(len(flattened)), word_divs[1:])
            return dict(zip(map(tuple, groups), nums))

        def get_new_to_old_map(old: np.ndarray, new: np.ndarray) -> dict[int, int]:
            old_cells_nums = get_cells_to_nums(old)
            new_cells_nums = get_cells_to_nums(new)
            new_to_old = {}
            for cells in set(old_cells_nums.keys()).intersection(new_cells_nums.keys()):
                if old_cells_nums[cells] and new_cells_nums[cells]:
                    new_to_old[new_cells_nums[cells]] = old_cells_nums[cells]
            return new_to_old

        across_new_old_map = get_new_to_old_map(old_across, self._across)
        down_new_old_map = get_new_to_old_map(old_down.T, self._down.T)

        new_words = {}
        for across_num in set(self._across.ravel()) - {0}:
            old_word = self._words.get((ACROSS, across_new_old_map.get(across_num)))
            new_words[(ACROSS, across_num)] = Word(
                self,
                ACROSS,
                across_num,
                clue=old_word.clue if old_word is not None else "",
            )
        for down_num in set(self._down.ravel()) - {0}:
            old_word = self._words.get((DOWN, down_new_old_map.get(down_num)))
            new_words[(DOWN, down_num)] = Word(
                self, DOWN, down_num, clue=old_word.clue if old_word is not None else ""
            )
        self._words = new_words
        edge_list = {
            w.index: [c.index for c in w.crosses if c and c.is_open()]
            for w in self.iterwords(only_open=True)
        }
        self._dependency_graph = nx.from_dict_of_lists(edge_list)

    def _get_direction_numbers(self, direction: Direction) -> np.ndarray:
        """An array indicating the word number for each cell for a given direction.

        Args:
            direction: The desired direction.

        Returns:
            The grid of word numbers for each cell.
        """
        if direction == Direction.ACROSS:
            return self._across
        elif direction == Direction.DOWN:
            return self._down

    def _get_word_mask(self, word_index: WordIndex) -> np.ndarray:
        """A boolean mask that indicates which grid cells belong to a word.

        Args:
            word_index: The index of the desired word.

        Returns:
            The grid indicating which cells are in the input word.
        """
        word = self[word_index]
        return self._get_direction_numbers(word.direction) == word.number

    def get_word_cells(self, word_index: WordIndex) -> list[Cell]:
        """Gets the cells for a word index.

        Args:
            word_index: The word index.

        Returns:
            The list of Cells in the word.
        """
        return list(self._grid[self._get_word_mask(word_index)])

    def get_cell_number(self, cell_index: CellIndex) -> int | None:
        """Gets the crossword numeral at a given cell, if it exists.

        Args:
            cell_index: The index of the cell.

        Returns:
            The crossword number in that cell, if any.
        """
        number = self._numbers[cell_index]
        if number:
            return number

    def iterwords(
        self, direction: Direction | None = None, only_open: bool = False
    ) -> Iterator[Word]:
        """Method for iterating over the words in the crossword.

        Args:
            direction: If provided, limits the iterator to only the given direction.
            only_open: Whether to only return open words. Defaults to False.

        Yields:
            An iterator of Word objects. Ordered in standard crossword fashion
                (ascending numbers, across then down).
        """
        for word_index in sorted(self._words.keys()):
            if direction is None or direction == self[word_index].direction:
                if not only_open or self._words[word_index].is_open():
                    yield (self._words[word_index])

    def itercells(self) -> Iterator[Cell]:
        """Method for iterating over the cells in the crossword.

        Yields:
            An iterator of Cell objects. Ordered left to right, top to bottom.
        """
        for cell in self._grid.ravel():
            yield cell

    def get_indices(self, word_index: WordIndex) -> list[CellIndex]:
        """Gets the list of cell indices for a given word.

        Args:
            word_index: The index of the desired word.

        Returns:
            A list of cell indices that belong to the word.
        """
        return [
            (int(x[0]), int(x[1]))
            for x in np.argwhere(self._get_word_mask(word_index)).tolist()
        ]

    def get_word_at_index(self, index: CellIndex, direction: Direction) -> Word | None:
        """Gets the word that passes through a cell in a given direction.

        Args:
            index: The index of the cell.
            direction: The direction of the word.

        Returns:
            The word passing through the index in the provided direction. If the index
            corresponds to a black square, or there is no word in that direction (an
            unchecked light) this method returns None.
        """
        if self[index] != BLACK:
            number = self._get_direction_numbers(direction)[index]
            try:
                return self[direction, number]
            except IndexError:
                return None

    def set_word(self, word_index: WordIndex, value: str) -> None:
        """Sets a word to a new value.

        Args:
            word_index: The index of the word.
            value: The new value of the word.
        """
        if not isinstance(value, str) or len(self[word_index]) != len(value):
            raise ValueError
        direction = word_index[0]
        word_mask = self._get_word_mask(word_index)
        cells = self._grid[word_mask]
        cross_indices = [
            (direction.opposite, n) if n else None
            for n in self._get_direction_numbers(direction.opposite)[word_mask]
        ]
        for i in range(len(value)):
            cells[i].value = value[i]
            cross_index = cross_indices[i]
            edge = (word_index, cross_index) if cross_index else None
            if cells[i].value == EMPTY and edge:
                self._dependency_graph.add_edge(*edge)
            elif edge and edge in self._dependency_graph.edges:
                self._dependency_graph.remove_edge(*edge)
        for wi in [word_index] + cross_indices:
            if wi:
                word = self[wi]
                if not word.is_open() and wi in self._dependency_graph.nodes:
                    self._dependency_graph.remove_node(wi)

    def set_cell(self, index: CellIndex, value: CellValue) -> None:
        """Sets a cell to a new value.

        Args:
            index: The index of the cell.
            value: The new value of the cell.
        """
        cell: Cell = self._grid[index]
        if value == BLACK:
            cell.value = BLACK
            images = self.get_symmetric_cell_index(index, force_list=True)
            for image in images:
                self._grid[image].value = BLACK
            self._parse_grid()
        elif cell == BLACK:
            cell.value = value
            images = self.get_symmetric_cell_index(index, force_list=True)
            for image in images:
                if self._grid[image].value == BLACK:
                    self._grid[image].value = EMPTY
            self._parse_grid()
        else:
            cell.value = value
            words = (cell.get_parent_word(ACROSS), cell.get_parent_word(DOWN))
            if all([w is not None for w in words]):
                edge = tuple(w.index for w in words)
                if cell.value == EMPTY:
                    self._dependency_graph.add_edge(*edge)
                elif edge in self._dependency_graph.edges:
                    self._dependency_graph.remove_edge(*edge)
            for word in words:
                if (
                    word is not None
                    and not word.is_open()
                    and word.index in self._dependency_graph.nodes
                ):
                    self._dependency_graph.remove_node(word.index)

    def copy(self) -> Crossword:
        """Returns a copy of the current crossword, with all linked objects (Words and
        Cells) properly associated to the new object. Modifying the returned object will
        not affect the original object.

        Returns:
            A copy of the current Crossword object.
        """
        return copy.deepcopy(self)

    def get_disconnected_open_subgrids(self) -> list[list[WordIndex]]:
        """Returns a list of open subgrids, as represented by a list of words. An open
        subgrid is a set of words whose fill can in principle depend on each other. For
        instance, if the only the northwest and southeast corners are a puzzle are open,
        such that they can be filled completely independently, the words in those two
        areas will be returned as separate subgrids.

        Returns:
            A list of open subgrids.
        """

        return [
            sorted(list(cc)) for cc in nx.connected_components(self._dependency_graph)
        ]

    def hashable_state(
        self, word_indices: list[WordIndex]
    ) -> tuple[tuple[WordIndex, str], ...]:
        """Returns a list of tuple of (word index, current value) pairs in sorted order.
        This provides a hashable object describing the state of the grid which can be
        compared between different Crossword objects.

        Args:
            word_indices: The list of word indices of interest.

        Returns:
            A tuple of (word index, value) tuples
        """
        sorted_indices = sorted(word_indices)
        return tuple((i, self[i].value) for i in sorted_indices)

    def fill(
        self,
        word_list: WordList | None = None,
        timeout: float | None = 30.0,
        temperature: float = 0.0,
        score_filter: float | None = None,
        allow_repeats: bool = False,
    ) -> Crossword | None:
        """Searches for a possible fill, and returns the result as a new Crossword
        object. Uses a modified depth-first-search algorithm.

        Args:
            word_list: An optional word list to use instead of the default for the
                crossword.
            timeout: The maximum time in seconds to search before returning. Defaults to
                30. If None, will search until completion.
            temperature: A parameter to control randomness. Defaults to 0 (no
                randomness). Reasonable values are around 1.
            score_filter: A threshold to apply to the word list before filling.
            allow_repeats: Whether to allow words that already appear in the grid.
                Defaults to false.

        Returns:
            The filled Crossword. Returns None if the search is
                exhausted or the timeout is hit.
        """
        dead_end_states = set()
        subgraphs = self.get_disconnected_open_subgrids()
        start_time = time.time()
        word_list = word_list if word_list is not None else self.word_list
        if score_filter:
            word_list = word_list.score_filter(score_filter)
        xw = self.copy()

        def recurse_subgraph_fill(
            active_subgraph: list[WordIndex], display_context: Live
        ) -> bool:
            if xw.hashable_state(active_subgraph) in dead_end_states:
                return False
            num_matches = np.array(
                [len(word_list.find_matches(xw[i])) for i in active_subgraph]
            )
            noise = np.abs(np.random.normal(scale=num_matches)) * temperature
            word_to_match: Word = xw[active_subgraph[np.argmin(num_matches + noise)]]
            matches = word_to_match.find_matches(word_list, allow_repeats=allow_repeats)
            if not matches:
                dead_end_states.add(xw.hashable_state(active_subgraph))
                return False
            else:
                noisy_matches = matches.rescore(
                    lambda _, s: s * np.random.lognormal(0.0, 0.1 * temperature)
                )
                old_value = word_to_match.value
                # temp fill for subgraph calculation
                xw[word_to_match.index] = noisy_matches.words[0]
                display_context.update(xw._text_grid())
                new_subgraphs = [
                    s
                    for s in xw.get_disconnected_open_subgrids()
                    if set(s).issubset(set(active_subgraph))
                ]
                for match in noisy_matches.words:
                    if timeout and time.time() > start_time + timeout:
                        xw[word_to_match.index] = old_value
                        return False
                    xw[word_to_match.index] = match
                    display_context.update(xw._text_grid())

                    for new_subgraph in sorted(new_subgraphs, key=len):
                        if not recurse_subgraph_fill(new_subgraph, display_context):
                            break
                    else:
                        return True
                xw[word_to_match.index] = old_value
                dead_end_states.add(xw.hashable_state(active_subgraph))
                return False

        with Live(self._text_grid(), refresh_per_second=4, transient=True) as live:
            for subgraph in sorted(subgraphs, key=len):
                if recurse_subgraph_fill(subgraph, live):
                    live.update(xw._text_grid(), refresh=True)
                else:
                    return
            else:
                return xw

    def _text_grid(self, numbers: bool = False) -> Table:
        """Returns a rich Table that displays the crossword.

        Args:
            numbers: If True, prints the numbers in the grid rather
                than the letters. Defaults to False.

        Returns:
            A Table object containing the crossword.
        """
        table = Table(
            box=rich.box.SQUARE,
            show_header=False,
            show_lines=True,
            width=4 * self.num_cols + 1,
            padding=0,
        )
        for c in range(self.num_cols):
            table.add_column(justify="left", width=3)
        for row in self._grid:
            strings = []
            for cell in row:
                if cell == SpecialCellValue.BLACK:
                    strings.append(cell.str * 3)
                else:
                    if numbers:
                        strings.append(str(cell.number) if cell.number else "")
                    else:
                        strings.append(
                            f"{'^' if cell.number else ' '}{cell.str}{'*' if cell.shaded or cell.circled else ' '}"
                        )
            table.add_row(*strings)

        return table

    def pprint(self, numbers: bool = False) -> str:
        """Prints a formatted string representation of the crossword fill.

        Args:
            numbers (bool): If True, prints the numbers in the grid rather
                than the letters. Defaults to False.
        """
        console = Console()
        console.print(self._text_grid(numbers))

    def _repr_mimebundle_(
        self, include: Iterable[str], exclude: Iterable[str], **kwargs: Any
    ) -> dict[str, str]:
        """A display method that handles different IPython environments.

        Args:
            include: MIME types to include.
            exclude: MIME types to exclude.

        Returns:
            A dict containing the outputs.
        """

        html = self._grid_html()
        text = self._text_grid()._repr_mimebundle_([], [])["text/plain"]
        data = {"text/plain": text, "text/html": html}
        if include:
            data = {k: v for (k, v) in data.items() if k in include}
        if exclude:
            data = {k: v for (k, v) in data.items() if k not in exclude}
        return data

    def _grid_html(self, size_px: int | None = None) -> str:
        """Returns an HTML rendering of the puzzle.

        Args:
            size_px: The size of the largest dimension in pixels. If None
                provided, defaults to the display_size_px property.

        Returns:
            HTML to display the puzzle.
        """
        size_px = size_px or self.display_size_px
        # Random suffix is a hack to ensure correct display in Jupyter settings
        suffix = token_hex(4)
        cells = []
        for c in self.itercells():
            c.number
            cell_number_span = f'<span class="cell-number">{c.number or ""}</span>'
            letter_span = f'<span class="letter">{c.str if c!=BLACK else ""}</span>'
            circle_span = '<span class="circle"></span>'
            if c == BLACK:
                extra_class = " black"
            elif c.shaded:
                extra_class = " gray"
            else:
                extra_class = ""
            cell_div = f"""
            <div class="crossword-cell{suffix}{extra_class}">
                {cell_number_span}
                {letter_span}
                {circle_span if c.circled else ""}
            </div>
            """
            cells.append(cell_div)
        aspect_ratio = self.num_rows / self.num_cols
        cell_size = size_px / max(self.num_rows, self.num_cols)
        css = CSS_TEMPLATE.format(
            num_cols=self.num_cols,
            height=size_px * min(1, aspect_ratio),
            width=size_px * min(1, 1 / aspect_ratio),
            num_font_size=int(cell_size * 0.3),
            val_font_size=int(cell_size * 0.6),
            circle_dim=cell_size - 1,
            suffix=suffix,
        )
        cells_html = "\n".join(cells)
        return f"""
        <div>
            <style scoped>
                {css}
            </style>
            <div class="crossword{suffix}">
                {cells_html}
            </div>
        </div>
        """
