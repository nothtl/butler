"""Unit tests for the pure deterministic scheduler (butler/schedule.py).

Run:
    python tests/test_schedule.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler import schedule as sch


def _task(id, remaining, deadline=None, priority=3, status="todo"):
    return sch.Task(id=id, title=f"task{id}", remaining_minutes=remaining,
                    deadline=deadline, priority=priority, status=status)


def _ev(id, start, end, source="local"):
    return sch.Event(id=id, title=f"event{id}", start_min=start, end_min=end,
                     source=source)


# A day window 09:00 -> 22:00 (540..1320), sleep 23:00-07:00.
DAY_START, DAY_END = 540, 1320
SLEEP = (1380, 420)


class TestGeometry(unittest.TestCase):
    def test_free_intervals_no_events(self):
        gaps = sch.free_intervals(DAY_START, DAY_END, *SLEEP, [])
        self.assertEqual(gaps, [(DAY_START, DAY_END)])

    def test_lecture_split(self):
        # lecture 10:00-11:30 (600..690)
        gaps = sch.free_intervals(DAY_START, DAY_END, *SLEEP, [_ev(1, 600, 690)])
        self.assertEqual(gaps, [(DAY_START, 600), (690, DAY_END)])

    def test_sleep_blocks_day(self):
        # day window crossing 22:00-07:00 -> sleep 1380.. (0..420) clipped
        blocks = sch.sleep_blocks(0, 1440, 1380, 420)
        self.assertEqual(blocks, [(1380, 1440), (0, 420)])

    def test_overlapping_events_merge(self):
        a = _ev(1, 600, 690)
        b = _ev(2, 700, 720)   # non-overlap -> two gaps still
        gaps = sch.free_intervals(DAY_START, DAY_END, *SLEEP, [a, b])
        self.assertEqual(gaps[-1], (720, DAY_END))


class TestCoreSolver(unittest.TestCase):
    def test_never_overlaps_event(self):
        lecture = _ev(1, 600, 690)
        plan = sch.solve(DAY_START, DAY_END, [lecture], [_task(1, 120)],
                         buffer_fraction=0.0, buffer_minutes=0,
                         min_slot_minutes=25, sleep_start=1380, sleep_end=420)
        for sl in plan.slots:
            self.assertIsNone(sch.overlap_with_event(sl, [lecture]),
                              f"slot {sl} overlaps lecture")

    def test_buffer_never_fills_100(self):
        tasks = [_task(i, 9999) for i in range(1, 6)]
        plan = sch.solve(DAY_START, DAY_END, [], tasks,
                         buffer_fraction=0.20, buffer_minutes=0,
                         min_slot_minutes=25, sleep_start=1380, sleep_end=420)
        total = sum(sl.end_min - sl.start_min for sl in plan.slots)
        avail = DAY_END - DAY_START
        self.assertLessEqual(total, int(avail * 0.80) + 1)
        self.assertLess(total, avail)

    def test_deadline_priority(self):
        # taskA deadline soon (12:00=720), taskB no deadline
        a = _task(1, 60, deadline=720, priority=3)
        b = _task(2, 60, deadline=None, priority=1)
        plan = sch.solve(DAY_START, DAY_END, [], [a, b],
                         buffer_fraction=0, buffer_minutes=0,
                         min_slot_minutes=25, sleep_start=1380, sleep_end=420)
        slots = sorted(plan.slots, key=lambda s: s.start_min)
        self.assertEqual(slots[0].task_id, 1, "deadline task should be first")

    def test_never_schedules_during_sleep(self):
        # 90-min task, only room late in the day
        plan = sch.solve(DAY_START, DAY_END, [], [_task(1, 120)],
                         buffer_fraction=0, buffer_minutes=0,
                         min_slot_minutes=25,
                         sleep_start=DAY_END - 30, sleep_end=420)
        for sl in plan.slots:
            self.assertLess(sl.end_min, DAY_END - 30)

    def test_partial_availability(self):
        # 90-min task but only a 60-min gap 10:00-11:00 -> partial, extends later
        gap_event = _ev(1, 600, 660)  # blocks 10:00-11:00? no: event occupies it.
        # Instead, make a 60-min free gap by bounding with events around.
        ev = [_ev(1, 540, 600), _ev(2, 660, 1320)]   # free gap 600..660 (60m)
        plan = sch.solve(DAY_START, DAY_END, ev, [_task(1, 90)],
                         buffer_fraction=0, buffer_minutes=0,
                         min_slot_minutes=25, sleep_start=1380, sleep_end=420)
        fitted = [s for s in plan.slots]
        self.assertTrue(len(fitted) >= 1)
        self.assertTrue(any(s.partial for s in fitted), "should be partial")

    def test_min_slot_respected(self):
        # only a 10-min gap (600..610) -> below min_slot, nothing schedulable
        ev = [_ev(1, 540, 600), _ev(2, 610, 1320)]
        plan = sch.solve(DAY_START, DAY_END, ev, [_task(1, 60)],
                         buffer_fraction=0, buffer_minutes=0,
                         min_slot_minutes=25, sleep_start=1380, sleep_end=420)
        self.assertEqual(plan.slots, [])


class TestDiff(unittest.TestCase):
    def test_moved_and_new(self):
        old = [sch.Slot(1, "a", 600, 660), sch.Slot(2, "b", 660, 720)]
        new = [sch.Slot(1, "a", 700, 760), sch.Slot(3, "c", 760, 810)]
        moved = sch.diff_old(old, new)
        by_id = {m["task_id"]: m for m in moved}
        self.assertIn(1, by_id)              # moved
        self.assertEqual(by_id[1]["reason"], "free time was needed earlier (or a hard event blocked the old time)")
        self.assertIn(3, by_id)              # newly scheduled
        self.assertEqual(by_id[3]["new"], (760, 810))
        self.assertIn(2, by_id)              # removed
        self.assertIsNone(by_id[2]["new"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
