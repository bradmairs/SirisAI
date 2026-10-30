"""Backfill from Apple Health's own export (Health app → your picture →
Export All Health Data → export.zip), for years of history in one go.

The zip holds `apple_health_export/export.xml`, one <Record> per sample and
one <Workout> per workout. It's read as a stream, so a multi-gigabyte file
doesn't need the memory.

Samples are raw: the phone and the watch both count steps, so adding up
every record would double them. For each day and metric that adds up
(steps, energy, distance...), only the device with the biggest total that
day counts, which is close to what the Health app shows. Rates and heart
figures are averaged; body measurements take the day's last. Sleep stages
are added up per night (the morning you woke up), also from one device.

The result has the same shape as auto_export.parse(), so the store treats
it like any other import (source "apple_export", below Health Auto Export
in the default priority, so it only fills the days that are missing).
"""

from __future__ import annotations

import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any
from xml.etree.ElementTree import iterparse

from app.health.auto_export import combine, convert, workout_entry

TYPES: dict[str, tuple[str, str]] = {
    "HKQuantityTypeIdentifierStepCount": ("steps", "count"),
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_energy_kcal", "kcal"),
    "HKQuantityTypeIdentifierBasalEnergyBurned": ("basal_energy_kcal", "kcal"),
    "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_minutes", "min"),
    "HKQuantityTypeIdentifierDistanceWalkingRunning": ("walk_run_km", "km"),
    "HKQuantityTypeIdentifierFlightsClimbed": ("flights", "count"),
    "HKQuantityTypeIdentifierRestingHeartRate": ("resting_heart_rate", "count/min"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("heart_rate_variability", "ms"),
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": ("walking_heart_rate", "count/min"),
    "HKQuantityTypeIdentifierRespiratoryRate": ("respiratory_rate", "count/min"),
    "HKQuantityTypeIdentifierOxygenSaturation": ("blood_oxygen", "%"),
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature": ("wrist_temperature", "degC"),
    "HKQuantityTypeIdentifierVO2Max": ("vo2_max", "ml/kg·min"),
    "HKQuantityTypeIdentifierBodyMass": ("weight_kg", "kg"),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("body_fat_pct", "%"),
    "HKQuantityTypeIdentifierLeanBodyMass": ("lean_mass_kg", "kg"),
    "HKQuantityTypeIdentifierDietaryWater": ("water_ml", "mL"),
    "HKQuantityTypeIdentifierDietaryCaffeine": ("caffeine_mg", "mg"),
}
SLEEP = {
    "HKCategoryValueSleepAnalysisAsleepDeep": "sleep_deep_h",
    "HKCategoryValueSleepAnalysisAsleepREM": "sleep_rem_h",
    "HKCategoryValueSleepAnalysisAsleepCore": "sleep_core_h",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "sleep_hours",
    "HKCategoryValueSleepAnalysisAsleep": "sleep_hours",
    "HKCategoryValueSleepAnalysisAwake": "sleep_awake_h",
    "HKCategoryValueSleepAnalysisInBed": "in_bed_h",
}
ADDS_UP = {"steps", "active_energy_kcal", "basal_energy_kcal", "exercise_minutes", "walk_run_km", "flights", "water_ml", "caffeine_mg"}


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(text)[:25], "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _unit_convert(metric: str, unit: str, value: float) -> float:
    u = unit.lower()
    if metric == "walk_run_km" and u == "m":
        return value / 1000
    if metric == "walk_run_km" and u == "mi":
        return value * 1.609344
    if metric == "water_ml" and u == "l":
        return value * 1000
    return convert(metric, unit, value)


def parse_xml(stream: IO[bytes], since: str | None = None) -> dict[str, Any]:
    # (day, metric) -> device -> values
    by_device: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    sleep_by_device: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    sleep_span: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    workouts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, el in iterparse(stream, events=("end",)):
        tag = el.tag
        if tag == "Record":
            kind = el.get("type", "")
            start = _when(el.get("startDate"))
            if start is None:
                el.clear()
                continue
            device = el.get("sourceName") or "?"
            if kind == "HKCategoryTypeIdentifierSleepAnalysis":
                end = _when(el.get("endDate"))
                metric = SLEEP.get(el.get("value", ""))
                if end and metric:
                    day = end.date().isoformat()  # the morning you woke up
                    if not since or day >= since:
                        sleep_by_device[(day, device)][metric] += (end - start).total_seconds() / 3600
                        if metric not in ("in_bed_h", "sleep_awake_h"):
                            sleep_span[(day, device)] += [start, end]
            elif kind in TYPES:
                metric, _ = TYPES[kind]
                day = start.date().isoformat()
                if not since or day >= since:
                    try:
                        value = _unit_convert(metric, el.get("unit", ""), float(el.get("value", "")))
                    except ValueError:
                        value = None
                    if value is not None:
                        by_device[(day, metric)][device].append(value)
        elif tag == "Workout":
            start = _when(el.get("startDate"))
            if start is not None and (not since or start.date().isoformat() >= since):
                name = (el.get("workoutActivityType") or "Workout").removeprefix("HKWorkoutActivityType")
                duration = float(el.get("duration") or 0)
                unit = (el.get("durationUnit") or "min").lower()
                seconds = duration * 60 if unit.startswith("min") else duration * 3600 if unit.startswith("h") else duration
                km = float(el.get("totalDistance") or 0) or None
                if km and (el.get("totalDistanceUnit") or "km").lower() == "mi":
                    km *= 1.609344
                kcal = float(el.get("totalEnergyBurned") or 0) or None
                if kcal and (el.get("totalEnergyBurnedUnit") or "kcal").lower() == "kj":
                    kcal /= 4.184
                workouts[start.date().isoformat()].append(workout_entry(name, start.strftime("%Y-%m-%d %H:%M:%S %z"), seconds, km, kcal))
        if tag in ("Record", "Workout"):
            el.clear()

    metrics: dict[str, dict[str, float]] = defaultdict(dict)
    for (day, metric), devices in by_device.items():
        if metric in ADDS_UP:
            metrics[day][metric] = round(max(sum(v) for v in devices.values()), 2)
        else:
            metrics[day][metric] = combine(metric, [x for v in devices.values() for x in v])
    nights: dict[str, tuple[str, dict[str, float]]] = {}
    for (day, device), stages in sleep_by_device.items():
        asleep = sum(h for m, h in stages.items() if m not in ("in_bed_h", "sleep_awake_h"))
        if day not in nights or asleep > sum(h for m, h in nights[day][1].items() if m not in ("in_bed_h", "sleep_awake_h")):
            nights[day] = (device, dict(stages))
    sleep_times: dict[str, dict[str, str]] = {}
    for day, (device, stages) in nights.items():
        asleep = sum(h for m, h in stages.items() if m not in ("in_bed_h", "sleep_awake_h"))
        if asleep:
            metrics[day]["sleep_hours"] = round(asleep, 2)
        for m in ("sleep_deep_h", "sleep_rem_h", "sleep_core_h", "sleep_awake_h", "in_bed_h"):
            if stages.get(m):
                metrics[day][m] = round(stages[m], 2)
        span = sleep_span.get((day, device))
        if span:
            sleep_times[day] = {"start": min(span).strftime("%Y-%m-%d %H:%M:%S %z"), "end": max(span).strftime("%Y-%m-%d %H:%M:%S %z")}
    for day, ws in workouts.items():
        runs = sum(w["km"] or 0 for w in ws if "running" in w["name"].lower())
        if runs:
            metrics[day]["running_km"] = round(runs, 2)
    return {"metrics": dict(metrics), "sleep": sleep_times, "workouts": dict(workouts), "ignored": []}


def parse_file(path: Path, since: str | None = None) -> dict[str, Any]:
    """export.zip (or a bare export.xml)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            name = next((n for n in archive.namelist() if n.endswith("/export.xml") or n == "export.xml"), None)
            if name is None:
                raise ValueError("That zip has no export.xml in it: export it again from the Health app.")
            with archive.open(name) as stream:
                return parse_xml(stream, since)
    with open(path, "rb") as stream:
        return parse_xml(stream, since)


def default_since(years: int = 5) -> str:
    return (datetime.now() - timedelta(days=365 * years)).date().isoformat()
