"""
GoPro Clip Renamer
==================

A tiny desktop app for coaches. It renames GoPro video files IN PLACE
(no copying, no uploading) to a meaningful, consistent naming convention,
e.g.

    2026-11-06_JaneSmith_turn_BackToBreast_01.MP4

Workflow for the coach (4 steps, all on one screen):
    1. Choose the folder that holds the clips (SD card or a folder on the PC).
    2. Tick the clips that belong together (just click a clip to tick it,
       click again to untick; Shift+click ticks a whole range; double-click
       plays the clip so you can check which one it is).
    3. Type the athlete's name, pick the skill + variant, click "Assign".
       (Repeat for the next athlete / next batch.)
    4. Check the preview table, click "Rename files now".

The turn numbers are NOT typed in: they are counted automatically from the
selection, and they continue from where they left off if the same athlete
does more reps later.

HOW THIS FILE IS ORGANISED (so it is easy to maintain)
------------------------------------------------------
    SECTION 1  CONFIGURATION   <- the only part you normally need to edit
    SECTION 2  CORE LOGIC      <- pure functions, no GUI, easy to test
    SECTION 3  GUI             <- everything the coach sees
    SECTION 4  ENTRY POINT
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# =============================================================================
# SECTION 1 - CONFIGURATION  (edit these to change the naming convention)
# =============================================================================

# The file name is built from these parts, in this order, joined by SEPARATOR.
# Re-order, add or remove parts here and EVERYTHING updates (preview, numbering,
# renaming). Allowed part names: "date", "athlete", "skill", "variant", "number".
# "number" must always be included (it is what keeps names unique).
# Empty parts (e.g. no variant) are skipped cleanly - no double underscores.
FILENAME_PARTS = ["date", "athlete", "skill", "variant", "number"]

# Character placed between the parts.
SEPARATOR = "_"

# How the date is written. "%Y-%m-%d" -> 2026-11-06 (sorts correctly in Explorer).
# Other examples: "%d%m%Y" -> 06112026, "%y%m%d" -> 261106.
DATE_FORMAT = "%Y-%m-%d"

# Turn/rep number is zero-padded to this many digits (2 -> 01, 02 ... 10).
# Padding keeps files in the correct order in Windows Explorer.
NUMBER_PADDING = 2

# What to do with spaces typed inside a part: "Jane Smith" -> "JaneSmith".
# Use "-" to get "Jane-Smith", or " " to keep spaces.
SPACE_REPLACEMENT = ""

# Skills offered in the drop-down, and the variants offered for each skill.
# The skill text itself is what goes into the file name ("turn", "start" ...).
# The coach can still type something that is not in the list.
SKILLS: dict[str, list[str]] = {
    "turn": ["BackToBreast", "BreastToBack", "Freestyle", "Backstroke",
             "Breaststroke", "Butterfly"],
    "start": ["Block", "Backstroke", "Relay"],
    "stroke": ["Freestyle", "Backstroke", "Breaststroke", "Butterfly"],
}

# Only these file types are listed (lower case, with the dot).
VIDEO_EXTENSIONS = {".mp4", ".mov"}

# GoPro's own names look like GX010123, GH010123, GOPR0123, GP010123.
# Files that no longer look like this are assumed to be renamed already and are
# hidden by default (the coach can tick "Show all video files" to see them).
ORIGINAL_NAME_PATTERN = re.compile(r"^(G[A-Z]\d{6}|GOPR\d{4}|GP\d{6})$", re.IGNORECASE)

# A log of every rename is appended to this file inside the clip folder, so
# anything can be traced back to the original name even after closing the app.
LOG_FILE_NAME = "gopro_rename_log.csv"


# =============================================================================
# SECTION 2 - CORE LOGIC  (no GUI code in here)
# =============================================================================

# Fields the template is allowed to use; used to catch typos in FILENAME_PARTS.
KNOWN_FIELDS = {"date", "athlete", "skill", "variant", "number"}

# Temporary stand-in for the number while we work out the "pattern" of a name.
_NUMBER_TOKEN = "<<NUMBER>>"

# Characters Windows does not allow in file names.
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


@dataclass(frozen=True)
class RenameJob:
    """One planned rename: which file, what it will be called, which batch."""
    source: Path       # full path of the existing file
    new_name: str      # new file name INCLUDING extension
    group_id: int      # clips assigned together share a group id

    @property
    def target(self) -> Path:
        """Full path the file will have after the rename (same folder)."""
        return self.source.with_name(self.new_name)


def validate_config() -> None:
    """Fail early, with a readable message, if the configuration is broken.

    Why: a typo such as "atlete" in FILENAME_PARTS would otherwise silently
    produce wrong file names. Better to refuse to start.
    """
    unknown = [p for p in FILENAME_PARTS if p not in KNOWN_FIELDS]
    if unknown:
        raise ValueError(f"Unknown part(s) in FILENAME_PARTS: {unknown}. "
                         f"Allowed: {sorted(KNOWN_FIELDS)}")
    if len(set(FILENAME_PARTS)) != len(FILENAME_PARTS):
        raise ValueError("FILENAME_PARTS contains the same part twice.")
    if "number" not in FILENAME_PARTS:
        raise ValueError('FILENAME_PARTS must include "number", otherwise '
                         "several files would get the same name.")
    # Make sure the date format is usable.
    dt.datetime.now().strftime(DATE_FORMAT)


def clean_component(text: str) -> str:
    """Make one piece of text safe to use inside a Windows file name."""
    text = text.strip()
    text = re.sub(r"\s+", SPACE_REPLACEMENT, text)        # spaces
    text = _INVALID_FILENAME_CHARS.sub("", text)          # illegal characters
    return text.rstrip(". ")                               # Windows dislikes trailing dots


def build_stem(fields: dict[str, str], number_text: str) -> str:
    """Build a file name WITHOUT extension from the template.

    `fields` holds date/athlete/skill/variant; `number_text` is the already
    formatted number (e.g. "03"). Empty parts are left out entirely.
    """
    values = dict(fields)
    values["number"] = number_text
    pieces = []
    for part in FILENAME_PARTS:
        value = values[part] if part == "number" else clean_component(values.get(part, ""))
        if value:
            pieces.append(value)
    return SEPARATOR.join(pieces)


def next_number(fields: dict[str, str], taken_stems: set[str]) -> int:
    """Find the next free turn number for this athlete/skill/variant/date.

    It looks at names that already exist (on disk or queued in this session),
    keeps only those that match the SAME pattern except for the number, and
    returns highest number + 1. So if turns 01-06 exist, the next batch starts
    at 07. With nothing existing it returns 1.
    """
    # Build the name once with a placeholder, then turn it into a regex where the
    # placeholder becomes "(\d+)" and everything else must match literally.
    template = build_stem(fields, _NUMBER_TOKEN)
    pattern = re.compile(
        r"(\d+)".join(re.escape(p) for p in template.split(_NUMBER_TOKEN)),
        re.IGNORECASE,  # Windows file names are case-insensitive
    )
    highest = 0
    for stem in taken_stems:
        match = pattern.fullmatch(stem)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def plan_group(files: list[Path], fields: dict[str, str],
               taken_stems: set[str], group_id: int) -> list[RenameJob]:
    """Turn an ordered list of clips into rename jobs with sequential numbers."""
    start = next_number(fields, taken_stems)
    jobs = []
    for offset, path in enumerate(files):
        number_text = str(start + offset).zfill(NUMBER_PADDING)
        stem = build_stem(fields, number_text)
        jobs.append(RenameJob(path, stem + path.suffix, group_id))
    return jobs


def validate_jobs(jobs: list[RenameJob]) -> list[str]:
    """Check a whole batch BEFORE touching any file. Returns a list of problems."""
    problems = []
    seen: dict[str, Path] = {}
    for job in jobs:
        if not job.source.exists():
            problems.append(f"Missing file (moved or deleted?): {job.source.name}")
        key = job.new_name.casefold()
        if key in seen:
            problems.append(f"Two files would both be called {job.new_name}")
        seen[key] = job.source
        if job.target.exists() and job.target != job.source:
            problems.append(f"A file called {job.new_name} already exists")
    return problems


def apply_renames(jobs: list[RenameJob]) -> list[tuple[Path, Path]]:
    """Rename all files. If ANY rename fails, put everything back and re-raise.

    All-or-nothing behaviour means the coach never ends up with a half-renamed
    folder. Returns the (old_path, new_path) pairs that were done.
    """
    done: list[tuple[Path, Path]] = []
    try:
        for job in jobs:
            if job.target.exists():  # os.rename could overwrite on some systems
                raise FileExistsError(f"{job.new_name} already exists")
            job.source.rename(job.target)
            done.append((job.source, job.target))
    except Exception:
        _revert(done)
        raise
    return done


def _revert(done: list[tuple[Path, Path]]) -> None:
    """Move renamed files back to their old names (newest rename first)."""
    for old, new in reversed(done):
        if new.exists() and not old.exists():
            new.rename(old)


def undo_renames(done: list[tuple[Path, Path]]) -> None:
    """Public wrapper used by the Undo button."""
    _revert(done)


def write_log(folder: Path, done: list[tuple[Path, Path]]) -> None:
    """Append old -> new names to a CSV log in the clip folder.

    Logging must never stop a rename that already succeeded, so failures here
    (e.g. read-only SD card) are ignored.
    """
    try:
        log_path = folder / LOG_FILE_NAME
        is_new = not log_path.exists()
        with open(log_path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(["timestamp", "old_name", "new_name"])
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            for old, new in done:
                writer.writerow([stamp, old.name, new.name])
    except OSError:
        pass


def list_video_files(folder: Path) -> list[Path]:
    """All video files in the folder, oldest first (= order they were filmed).

    Sorted by modified time, then name. The turn number follows this order.
    """
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def all_stems(folder: Path) -> set[str]:
    """Names (without extension) of every file in the folder."""
    return {p.stem for p in folder.iterdir() if p.is_file()}


def open_with_default_player(path: Path) -> None:
    """Play a clip in whatever app the computer uses for videos."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # Windows only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# =============================================================================
# SECTION 3 - GUI
# =============================================================================

class RenamerApp(tk.Tk):
    """Main window. Holds the state and wires buttons to the core logic."""

    def __init__(self) -> None:
        super().__init__()
        self.title("GoPro Clip Renamer")
        self.geometry("1000x760")
        self.minsize(880, 640)

        # ---- State ---------------------------------------------------------
        self.folder: Path | None = None
        self.all_clips: list[Path] = []        # every video file in the folder
        self.visible_clips: list[Path] = []    # what the clip list currently shows
        self.ticked: set[Path] = set()         # clips the coach has ticked
        self._anchor: int | None = None        # last clicked row (for Shift+click)
        self._hidden_count = 0                 # already-renamed clips hidden
        self.queue: list[RenameJob] = []       # assigned, waiting for "Rename now"
        self.last_batch: list[tuple[Path, Path]] = []   # for Undo
        self.athletes: list[str] = []          # names typed this session
        self.next_group_id = 1

        # ---- Tk variables bound to input widgets ---------------------------
        self.folder_var = tk.StringVar(value="No folder chosen yet")
        self.date_var = tk.StringVar(value=dt.date.today().strftime(DATE_FORMAT))
        self.athlete_var = tk.StringVar()
        self.skill_var = tk.StringVar(value=next(iter(SKILLS)))
        self.variant_var = tk.StringVar()
        self.show_all_var = tk.BooleanVar(value=False)
        self.preview_var = tk.StringVar()
        self.status_var = tk.StringVar()

        self._build_widgets()
        self._on_skill_changed()
        self._update_buttons()

    # ------------------------------------------------------------------ build
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Step 1 - folder
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="1. Choose folder with GoPro clips...",
                   command=self.choose_folder).pack(side="left")
        ttk.Label(top, textvariable=self.folder_var).pack(side="left", padx=10)

        # Middle area: clip list (left) + description form (right)
        middle = ttk.Frame(self)
        middle.pack(fill="both", expand=True, **pad)
        middle.columnconfigure(0, weight=3)
        middle.columnconfigure(1, weight=2)
        middle.rowconfigure(0, weight=1)

        # Step 2 - clip checklist
        # Why a Treeview instead of a Listbox: it lets us show a tick box per
        # row, so the coach never needs Ctrl/Shift to select several clips.
        list_box = ttk.LabelFrame(
            middle, text="2. Tick the clips  (click = tick/untick, "
                         "Shift+click = tick a range, double-click = play)")
        list_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # Widgets at the bottom are packed FIRST so they always stay visible.
        ttk.Checkbutton(list_box, text="Show all video files",
                        variable=self.show_all_var,
                        command=self._refresh_clip_list).pack(side="bottom", anchor="w", padx=6)
        ttk.Label(list_box, textvariable=self.status_var).pack(side="bottom", anchor="w", padx=6)
        tick_buttons = ttk.Frame(list_box)
        tick_buttons.pack(side="bottom", fill="x", padx=6, pady=4)
        ttk.Button(tick_buttons, text="Tick all", command=self.tick_all).pack(side="left")
        ttk.Button(tick_buttons, text="Untick all", command=self.untick_all).pack(side="left", padx=6)

        self.clip_tree = ttk.Treeview(
            list_box, columns=("tick", "name", "filmed", "size"),
            show="headings", selectmode="none")   # "none": we draw our own ticks
        for column, title, width in (("tick", "", 34), ("name", "Clip", 130),
                                     ("filmed", "Filmed", 130), ("size", "Size", 80)):
            self.clip_tree.heading(column, text=title)
            self.clip_tree.column(column, width=width, anchor="w", stretch=(column == "name"))
        self.clip_tree.tag_configure("ticked", background="#d9ecff")
        scroll = ttk.Scrollbar(list_box, command=self.clip_tree.yview)
        self.clip_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=6)
        self.clip_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        self.clip_tree.bind("<Button-1>", self._on_clip_click)
        self.clip_tree.bind("<Double-Button-1>", self._play_clicked_clip)

        # Step 3 - description form
        form = ttk.LabelFrame(middle, text="3. Describe the ticked clips")
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text=f"Date ({dt.date(2026, 11, 6).strftime(DATE_FORMAT)}):").grid(
            row=0, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.date_var).grid(row=0, column=1, sticky="ew", **pad)

        ttk.Label(form, text="Athlete:").grid(row=1, column=0, sticky="w", **pad)
        self.athlete_box = ttk.Combobox(form, textvariable=self.athlete_var, values=[])
        self.athlete_box.grid(row=1, column=1, sticky="ew", **pad)

        ttk.Label(form, text="Skill:").grid(row=2, column=0, sticky="w", **pad)
        skill_box = ttk.Combobox(form, textvariable=self.skill_var, values=list(SKILLS))
        skill_box.grid(row=2, column=1, sticky="ew", **pad)
        skill_box.bind("<<ComboboxSelected>>", lambda _e: self._on_skill_changed())

        ttk.Label(form, text="Variant:").grid(row=3, column=0, sticky="w", **pad)
        self.variant_box = ttk.Combobox(form, textvariable=self.variant_var, values=[])
        self.variant_box.grid(row=3, column=1, sticky="ew", **pad)

        ttk.Label(form, text="The file name will look like:").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(14, 0))
        ttk.Label(form, textvariable=self.preview_var, wraplength=320,
                  foreground="#0b5394", justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", padx=8)

        self.assign_button = ttk.Button(form, text="Assign ticked clips  \u2193",
                                        command=self.assign_selected)
        self.assign_button.grid(row=6, column=0, columnspan=2, sticky="ew", padx=8, pady=14)

        # Re-compute the preview whenever the coach types or picks anything.
        for var in (self.date_var, self.athlete_var, self.skill_var, self.variant_var):
            var.trace_add("write", lambda *_a: self._update_preview())

        # Step 4 - queue and rename
        bottom = ttk.LabelFrame(self, text="4. Check, then rename")
        bottom.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(bottom, columns=("old", "new"), show="headings",
                                 height=8, selectmode="extended")
        self.tree.heading("old", text="Current name")
        self.tree.heading("new", text="New name")
        self.tree.column("old", width=240)
        self.tree.column("new", width=640)
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)

        buttons = ttk.Frame(bottom)
        buttons.pack(fill="x", padx=6, pady=(0, 6))
        self.remove_button = ttk.Button(
            buttons, text="Remove selected batch(es) from list",
            command=self.remove_selected_groups)
        self.remove_button.pack(side="left")
        self.undo_button = ttk.Button(buttons, text="Undo last rename",
                                      command=self.undo_last)
        self.undo_button.pack(side="left", padx=8)
        self.rename_button = ttk.Button(buttons, text="Rename files now",
                                        command=self.rename_now)
        self.rename_button.pack(side="right")

    # ------------------------------------------------------------- folder/list
    def choose_folder(self) -> None:
        """Step 1: pick the folder and load its clips."""
        if self.queue and not messagebox.askyesno(
                "Discard current list?",
                "Changing folder will clear the list below (nothing is renamed yet). Continue?"):
            return
        chosen = filedialog.askdirectory(title="Choose the folder with the GoPro clips")
        if not chosen:
            return
        self.folder = Path(chosen)
        self.queue.clear()
        self.last_batch.clear()
        self.folder_var.set(str(self.folder))
        try:
            self._rescan_folder()
        except OSError as error:
            messagebox.showerror("Cannot read folder", str(error))
            return
        # Pre-fill the date with the day the first clip was filmed.
        if self.all_clips:
            filmed = dt.datetime.fromtimestamp(self.all_clips[0].stat().st_mtime)
            self.date_var.set(filmed.strftime(DATE_FORMAT))
        self._refresh_queue_view()
        self._update_buttons()

    def _rescan_folder(self) -> None:
        """Re-read the folder from disk and refresh the clip list."""
        self.all_clips = list_video_files(self.folder) if self.folder else []
        self._refresh_clip_list()

    def _refresh_clip_list(self) -> None:
        """Fill the checklist with clips that are not queued (and, by default,
        still have their original GoPro name)."""
        queued = {job.source for job in self.queue}
        pending = [c for c in self.all_clips if c not in queued]
        if self.show_all_var.get():
            self.visible_clips = pending
        else:
            self.visible_clips = [c for c in pending if ORIGINAL_NAME_PATTERN.match(c.stem)]
        self._hidden_count = len(pending) - len(self.visible_clips)

        # Forget ticks for clips that are no longer shown (assigned / hidden).
        self.ticked &= set(self.visible_clips)
        self._anchor = None

        self.clip_tree.delete(*self.clip_tree.get_children())
        for index, clip in enumerate(self.visible_clips):
            info = clip.stat()
            filmed = dt.datetime.fromtimestamp(info.st_mtime).strftime("%d %b %H:%M:%S")
            size = f"{info.st_size / (1024 * 1024):.1f} MB"
            # The row id is the clip's position, so clicks map straight back to it.
            self.clip_tree.insert("", tk.END, iid=str(index),
                                  values=("\u2610", clip.name, filmed, size))
        self._redraw_ticks()

    def _redraw_ticks(self) -> None:
        """Show a tick box and a highlight on every ticked row, update the
        counter text and the file-name preview."""
        for index, clip in enumerate(self.visible_clips):
            is_ticked = clip in self.ticked
            self.clip_tree.set(str(index), "tick", "\u2611" if is_ticked else "\u2610")
            self.clip_tree.item(str(index), tags=("ticked",) if is_ticked else ())
        note = (f"{len(self.ticked)} ticked of {len(self.visible_clips)} "
                "clip(s) waiting to be assigned.")
        if self._hidden_count:
            note += f"  ({self._hidden_count} already-renamed hidden)"
        self.status_var.set(note)
        self._update_preview()

    def _on_clip_click(self, event: tk.Event) -> str:
        """Click toggles one clip; Shift+click ticks everything from the last
        clicked clip to this one."""
        row = self.clip_tree.identify_row(event.y)
        if row:
            index = int(row)
            if event.state & 0x0001 and self._anchor is not None:   # Shift held
                low, high = sorted((self._anchor, index))
                self.ticked.update(self.visible_clips[low:high + 1])
            else:
                self.ticked.symmetric_difference_update({self.visible_clips[index]})
                self._anchor = index
            self._redraw_ticks()
        return "break"   # stop Tk's default row-selection behaviour

    def tick_all(self) -> None:
        self.ticked = set(self.visible_clips)
        self._redraw_ticks()

    def untick_all(self) -> None:
        self.ticked.clear()
        self._redraw_ticks()

    def _play_clicked_clip(self, event: tk.Event) -> None:
        """Double-click plays the clip so the coach can see which one it is.

        A double-click is delivered as ONE event (the second click does not
        fire the normal click handler), so we toggle here once more to make
        the two clicks cancel out - the tick state ends up unchanged.
        """
        self._on_clip_click(event)
        row = self.clip_tree.identify_row(event.y)
        if row:
            try:
                open_with_default_player(self.visible_clips[int(row)])
            except OSError as error:
                messagebox.showerror("Cannot play clip", str(error))

    # ------------------------------------------------------------------ form
    def _on_skill_changed(self) -> None:
        """Offer the variants that belong to the chosen skill."""
        self.variant_box.configure(values=SKILLS.get(self.skill_var.get(), []))
        self.variant_var.set("")  # force a conscious choice (checked on Assign)

    def _read_fields(self) -> dict[str, str]:
        """Collect the form values, normalising the date if it is valid."""
        date_text = self.date_var.get().strip()
        try:
            date_text = dt.datetime.strptime(date_text, DATE_FORMAT).strftime(DATE_FORMAT)
        except ValueError:
            pass  # leave as typed; Assign will complain properly
        return {
            "date": date_text,
            "athlete": self.athlete_var.get(),
            "skill": self.skill_var.get(),
            "variant": self.variant_var.get(),
        }

    def _taken_stems(self) -> set[str]:
        """Every name already used on disk or queued, to continue numbering."""
        stems = all_stems(self.folder) if self.folder else set()
        stems |= {Path(job.new_name).stem for job in self.queue}
        return stems

    def _update_preview(self) -> None:
        """Show the coach exactly what the first (and last) file will be called."""
        if not self.folder:
            self.preview_var.set("Choose a folder first.")
            return
        fields = self._read_fields()
        if not clean_component(fields["athlete"]):
            self.preview_var.set("Type the athlete's name to see the file name.")
            return
        count = max(1, len(self.ticked))
        try:
            start = next_number(fields, self._taken_stems())
        except OSError:
            return
        first = build_stem(fields, str(start).zfill(NUMBER_PADDING))
        text = first + ".MP4"
        if count > 1:
            last = build_stem(fields, str(start + count - 1).zfill(NUMBER_PADDING))
            text += f"\n...\n{last}.MP4"
        self.preview_var.set(text)

    # ----------------------------------------------------------------- actions
    def assign_selected(self) -> None:
        """Step 3 action: move the selected clips into the rename list."""
        if not self.folder:
            messagebox.showinfo("Choose a folder", "Please choose a folder first (step 1).")
            return
        if not self.ticked:
            messagebox.showinfo("No clips ticked",
                                "Tick one or more clips in the list first (step 2).")
            return

        fields = self._read_fields()
        problem = self._validate_form(fields)
        if problem:
            messagebox.showwarning("Check the details", problem)
            return
        # A turn without its type is almost certainly a slip - ask to be sure.
        if SKILLS.get(fields["skill"]) and not clean_component(fields["variant"]):
            if not messagebox.askyesno(
                    "Variant is empty",
                    "You did not choose a variant. Continue without one?"):
                return

        # Keep filmed order (list order), whatever order the coach clicked in.
        files = [clip for clip in self.visible_clips if clip in self.ticked]
        jobs = plan_group(files, fields, self._taken_stems(), self.next_group_id)
        self.next_group_id += 1
        self.queue.extend(jobs)

        athlete = fields["athlete"].strip()
        if athlete and athlete not in self.athletes:
            self.athletes.append(athlete)
            self.athlete_box.configure(values=self.athletes)

        self._refresh_clip_list()
        self._refresh_queue_view()
        self._update_buttons()

    def _validate_form(self, fields: dict[str, str]) -> str:
        """Return a message describing what is wrong with the form, or ''."""
        try:
            dt.datetime.strptime(fields["date"], DATE_FORMAT)
        except ValueError:
            example = dt.date(2026, 11, 6).strftime(DATE_FORMAT)
            return f"The date must look like {example}."
        if not clean_component(fields["athlete"]):
            return "Please type the athlete's name."
        if not clean_component(fields["skill"]):
            return "Please choose or type the skill (turn / start / stroke)."
        return ""

    def remove_selected_groups(self) -> None:
        """Take whole batches back out of the list.

        Whole batches (not single rows) are removed so the numbering of the
        remaining clips never gets a gap.
        """
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected", "Click a row in the list first.")
            return
        groups = {self.queue[int(iid)].group_id for iid in selected}
        self.queue = [job for job in self.queue if job.group_id not in groups]
        self._refresh_clip_list()
        self._refresh_queue_view()
        self._update_buttons()

    def rename_now(self) -> None:
        """Step 4 action: check everything, then rename all queued files."""
        if not self.queue or not self.folder:
            return
        problems = validate_jobs(self.queue)
        if problems:
            messagebox.showerror("Cannot rename yet",
                                 "Nothing was changed.\n\n" + "\n".join(problems))
            return
        if not messagebox.askyesno(
                "Rename files",
                f"Rename {len(self.queue)} file(s)?\n\n"
                "The videos stay where they are. You can undo this afterwards."):
            return
        try:
            done = apply_renames(self.queue)
        except Exception as error:  # apply_renames already restored the old names
            messagebox.showerror("Rename failed",
                                 f"Nothing was changed.\n\nReason: {error}")
            return
        write_log(self.folder, done)
        self.last_batch = done
        self.queue.clear()
        self._rescan_folder()
        self._refresh_queue_view()
        self._update_buttons()
        messagebox.showinfo("Done", f"{len(done)} file(s) renamed.")

    def undo_last(self) -> None:
        """Put the most recent batch of renamed files back to their old names."""
        if not self.last_batch:
            return
        if not messagebox.askyesno("Undo",
                                   f"Restore the original names of {len(self.last_batch)} file(s)?"):
            return
        undo_renames(self.last_batch)
        self.last_batch = []
        self._rescan_folder()
        self._update_buttons()
        messagebox.showinfo("Undone", "The original file names were restored.")

    # ------------------------------------------------------------- view helpers
    def _refresh_queue_view(self) -> None:
        """Redraw the preview table from self.queue."""
        self.tree.delete(*self.tree.get_children())
        for index, job in enumerate(self.queue):
            self.tree.insert("", tk.END, iid=str(index),
                             values=(job.source.name, job.new_name))

    def _update_buttons(self) -> None:
        """Enable only the buttons that make sense right now."""
        self.rename_button.state(["!disabled"] if self.queue else ["disabled"])
        self.remove_button.state(["!disabled"] if self.queue else ["disabled"])
        self.undo_button.state(["!disabled"] if self.last_batch else ["disabled"])


# =============================================================================
# SECTION 4 - ENTRY POINT
# =============================================================================

def main() -> None:
    """Start the app (and show configuration mistakes in a friendly box)."""
    try:
        validate_config()
    except ValueError as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Configuration problem", str(error))
        return

    # On Windows, ask for sharp (not blurry) rendering on high-DPI screens.
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    RenamerApp().mainloop()


if __name__ == "__main__":
    main()