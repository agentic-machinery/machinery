"""Bounded Fleet Comms gauges for the existing Monitor Prometheus scrape."""

from prometheus_client.core import GaugeMetricFamily

from lu_machinery.api.comms_router import (
    _FAILURE_METRICS,
    COMMS_METRICS_TTL_S,
    cached_panel_metrics,
    warm_panel_metrics,
)
from lu_machinery.api.monitor_context import MonitorContext

COMMS_METRICS_MAX_AGE_S = COMMS_METRICS_TTL_S + 90.0


class CommsCollector:
    """Export the dashboard snapshot; event aggregation runs off the scrape path."""

    def __init__(self, context: MonitorContext) -> None:
        self.context = context

    def collect(self):
        stored = cached_panel_metrics(self.context)
        if stored is None or stored[1] >= COMMS_METRICS_TTL_S:
            warm_panel_metrics(self.context)
        payload, age = ({}, None) if stored is None else stored
        windows = payload.get("failure_windows", {})
        healthy = age is not None and age < COMMS_METRICS_MAX_AGE_S and payload.get("source") == "authority" and all(
            windows.get(label, {}).get(metric, {}).get("total") is not None
            for label in ("24h", "7d") for metric in _FAILURE_METRICS
        ) and payload.get("store", {}).get("reachable") is True
        health = GaugeMetricFamily(
            "lu_fleet_comms_collection_success", "Whether authority failure metrics have a snapshot younger than 120s.",
        )
        health.add_metric([], int(healthy))
        yield health
        snapshot_age = GaugeMetricFamily("lu_fleet_comms_snapshot_age_seconds", "Age of cached authority failure metrics.")
        if age is not None:
            snapshot_age.add_metric([], age)
        yield snapshot_age
        if not healthy:
            return
        for metric in _FAILURE_METRICS:
            total = GaugeMetricFamily(
                f"lu_fleet_comms_{metric}_recent", "Terminal events in the rolling window, cached for 30s.", labels=["window"],
            )
            grouped = GaugeMetricFamily(
                f"lu_fleet_comms_{metric}_recent_by_recipient", "Terminal events by bounded recipient bucket.",
                labels=["window", "recipient"],
            )
            for label in ("24h", "7d"):
                row = windows.get(label, {}).get(metric, {})
                if row.get("total") is None:
                    continue
                total.add_metric([label], row["total"])
                for recipient, count in row["by_recipient"].items():
                    grouped.add_metric([label, recipient], count)
            yield total
            yield grouped
