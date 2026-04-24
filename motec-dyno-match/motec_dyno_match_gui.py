#!/usr/bin/env python3
"""
GUI front-end for motec_dyno_match.

Lets you pick one or more dyno CSVs and one or more MoTeC CSVs, runs the
matcher across every (dyno, motec) combination, and shows results in a
sortable table. No external dependencies beyond tkinter (stdlib) and whatever
motec_dyno_match needs (pandas, numpy).

Run:
    python motec_dyno_match_gui.py
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from motec_dyno_match import match_runs, Match


# --------------------------------------------------------------------------- #
#  Worker                                                                     #
# --------------------------------------------------------------------------- #

class MatchJob:
    """A single (dyno, motec) combination queued to run on a worker thread."""

    def __init__(self, dyno_path: str, motec_path: str, threshold: float):
        self.dyno_path = dyno_path
        self.motec_path = motec_path
        self.threshold = threshold

    def run(self) -> tuple[str, str, list[Match], Optional[str]]:
        """Return (dyno, motec, matches, error_or_None)."""
        try:
            matches = match_runs(
                self.motec_path, self.dyno_path, threshold=self.threshold
            )
            return self.dyno_path, self.motec_path, matches, None
        except Exception as e:  # noqa: BLE001 — surface anything to UI
            return self.dyno_path, self.motec_path, [], str(e)


# --------------------------------------------------------------------------- #
#  UI                                                                         #
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MoTeC ↔ Dyno Matcher")
        self.geometry("1100x650")
        self.minsize(800, 500)

        self.dyno_paths: list[str] = []
        self.motec_paths: list[str] = []

        # Thread-safe queue for results from the worker.
        self._results_q: queue.Queue = queue.Queue()
        self._pending_jobs = 0

        self._build_ui()
        self._poll_results()

    # -- layout ---------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Top: file pickers (two columns)
        files_frame = ttk.Frame(self)
        files_frame.pack(fill="x", **pad)
        files_frame.columnconfigure(0, weight=1)
        files_frame.columnconfigure(1, weight=1)

        # --- Dyno side ---
        dyno_box = ttk.LabelFrame(files_frame, text="1. Dyno files")
        dyno_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.dyno_list = tk.Listbox(
            dyno_box, selectmode="extended", height=6, activestyle="none"
        )
        self.dyno_list.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        dyno_btns = ttk.Frame(dyno_box)
        dyno_btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(dyno_btns, text="Add…", command=self._add_dyno).pack(side="left")
        ttk.Button(dyno_btns, text="Remove selected", command=self._remove_dyno).pack(
            side="left", padx=4
        )
        ttk.Button(dyno_btns, text="Clear", command=self._clear_dyno).pack(side="left")

        # --- MoTeC side ---
        motec_box = ttk.LabelFrame(files_frame, text="2. MoTeC files")
        motec_box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.motec_list = tk.Listbox(
            motec_box, selectmode="extended", height=6, activestyle="none"
        )
        self.motec_list.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        motec_btns = ttk.Frame(motec_box)
        motec_btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(motec_btns, text="Add…", command=self._add_motec).pack(side="left")
        ttk.Button(motec_btns, text="Remove selected", command=self._remove_motec).pack(
            side="left", padx=4
        )
        ttk.Button(motec_btns, text="Clear", command=self._clear_motec).pack(side="left")

        # --- Controls row ---
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill="x", **pad)
        ttk.Label(ctrl_frame, text="Threshold:").pack(side="left")
        self.threshold_var = tk.DoubleVar(value=0.90)
        threshold_spin = ttk.Spinbox(
            ctrl_frame,
            from_=0.50,
            to=1.00,
            increment=0.05,
            textvariable=self.threshold_var,
            width=6,
            format="%.2f",
        )
        threshold_spin.pack(side="left", padx=(4, 12))

        self.run_btn = ttk.Button(ctrl_frame, text="Run match", command=self._run)
        self.run_btn.pack(side="left")
        ttk.Button(ctrl_frame, text="Clear results", command=self._clear_results).pack(
            side="left", padx=(6, 0)
        )

        self.status_var = tk.StringVar(value="Pick dyno and MoTeC files, then click Run match.")
        ttk.Label(ctrl_frame, textvariable=self.status_var).pack(side="right")

        # --- Results table ---
        res_frame = ttk.LabelFrame(self, text="Results")
        res_frame.pack(fill="both", expand=True, **pad)

        cols = ("dyno", "motec", "start", "end", "duration", "corr", "ratio")
        col_widths = {
            "dyno": 180, "motec": 180, "start": 90, "end": 90,
            "duration": 80, "corr": 70, "ratio": 70,
        }
        col_headings = {
            "dyno": "Dyno file",
            "motec": "MoTeC file",
            "start": "Start (s)",
            "end": "End (s)",
            "duration": "Duration (s)",
            "corr": "Correlation",
            "ratio": "Ratio",
        }

        self.tree = ttk.Treeview(res_frame, columns=cols, show="headings")
        for c in cols:
            self.tree.heading(c, text=col_headings[c], command=lambda cc=c: self._sort_by(cc))
            anchor = "w" if c in ("dyno", "motec") else "e"
            self.tree.column(c, width=col_widths[c], anchor=anchor)
        vsb = ttk.Scrollbar(res_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._sort_dir: dict[str, bool] = {c: False for c in cols}

    # -- file handling --------------------------------------------------------

    def _add_dyno(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select one or more dyno CSV files",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        for p in paths:
            if p and p not in self.dyno_paths:
                self.dyno_paths.append(p)
                self.dyno_list.insert("end", os.path.basename(p))

    def _add_motec(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select one or more MoTeC CSV files",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        for p in paths:
            if p and p not in self.motec_paths:
                self.motec_paths.append(p)
                self.motec_list.insert("end", os.path.basename(p))

    def _remove_selected(self, lb: tk.Listbox, paths: list[str]) -> None:
        # Delete in reverse to keep indices valid.
        for idx in reversed(lb.curselection()):
            lb.delete(idx)
            del paths[idx]

    def _remove_dyno(self) -> None:
        self._remove_selected(self.dyno_list, self.dyno_paths)

    def _remove_motec(self) -> None:
        self._remove_selected(self.motec_list, self.motec_paths)

    def _clear_dyno(self) -> None:
        self.dyno_list.delete(0, "end")
        self.dyno_paths.clear()

    def _clear_motec(self) -> None:
        self.motec_list.delete(0, "end")
        self.motec_paths.clear()

    # -- results --------------------------------------------------------------

    def _clear_results(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _sort_by(self, col: str) -> None:
        """Sort the tree by clicked column; toggle direction on repeat click."""
        numeric_cols = {"start", "end", "duration", "corr", "ratio"}
        items = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]

        def key(item):
            v = item[0]
            if col in numeric_cols:
                try:
                    return float(v)
                except ValueError:
                    return float("-inf")
            return v.lower()

        reverse = self._sort_dir[col]
        items.sort(key=key, reverse=reverse)
        for idx, (_, iid) in enumerate(items):
            self.tree.move(iid, "", idx)
        self._sort_dir[col] = not reverse

    # -- runner ---------------------------------------------------------------

    def _run(self) -> None:
        if not self.dyno_paths:
            messagebox.showwarning("Nothing to match", "Add at least one dyno file.")
            return
        if not self.motec_paths:
            messagebox.showwarning("Nothing to match", "Add at least one MoTeC file.")
            return
        if self._pending_jobs > 0:
            messagebox.showinfo("Already running", "A match job is already in progress.")
            return

        try:
            threshold = float(self.threshold_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Bad threshold", "Threshold must be a number between 0 and 1.")
            return
        if not (0.0 < threshold <= 1.0):
            messagebox.showerror("Bad threshold", "Threshold must be between 0 and 1.")
            return

        self._clear_results()
        self.run_btn.state(["disabled"])

        jobs = [
            MatchJob(dyno, motec, threshold)
            for dyno in self.dyno_paths
            for motec in self.motec_paths
        ]
        self._pending_jobs = len(jobs)
        self.status_var.set(f"Running {self._pending_jobs} combination(s)…")

        # Fire off worker threads. tkinter is not thread-safe, so the workers
        # only push results into a queue; the main thread polls it.
        for job in jobs:
            threading.Thread(target=self._worker, args=(job,), daemon=True).start()

    def _worker(self, job: MatchJob) -> None:
        self._results_q.put(job.run())

    def _poll_results(self) -> None:
        """Drain the results queue on the Tk main loop. Called every 100 ms."""
        try:
            while True:
                dyno_path, motec_path, matches, err = self._results_q.get_nowait()
                self._handle_result(dyno_path, motec_path, matches, err)
                self._pending_jobs -= 1
                if self._pending_jobs == 0:
                    self.run_btn.state(["!disabled"])
                    total = len(self.tree.get_children())
                    self.status_var.set(f"Done. {total} match(es) found.")
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_results)

    def _handle_result(
        self, dyno_path: str, motec_path: str, matches: list[Match], err: Optional[str]
    ) -> None:
        dyno_name = os.path.basename(dyno_path)
        motec_name = os.path.basename(motec_path)

        if err is not None:
            self.tree.insert(
                "",
                "end",
                values=(dyno_name, motec_name, "-", "-", "-", "ERROR", err[:40]),
            )
            return

        if not matches:
            self.tree.insert(
                "",
                "end",
                values=(dyno_name, motec_name, "-", "-", "-", "no match", "-"),
            )
            return

        for m in matches:
            self.tree.insert(
                "",
                "end",
                values=(
                    dyno_name,
                    motec_name,
                    f"{m.start:.2f}",
                    f"{m.end:.2f}",
                    f"{m.end - m.start:.2f}",
                    f"{m.corr:.4f}",
                    f"{m.ratio:.3f}",
                ),
            )


# --------------------------------------------------------------------------- #
#  Entry point                                                                #
# --------------------------------------------------------------------------- #

def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
