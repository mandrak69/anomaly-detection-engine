from anomaly_detection_engine.config import load_config, load_dotenv
from anomaly_detection_engine.observability.logging_config import configure_logging
from anomaly_detection_engine.pipeline import run_detection, run_ingestion
from anomaly_detection_engine.runtime import build_runtime


def main() -> None:
    configure_logging()

    load_dotenv()
    config = load_config()
    runtime = build_runtime(config)

    events = run_ingestion(runtime, config)
    summary = run_detection(runtime, events, config)

    print(f"Detection: {summary}")
    print(f"Metrics: {runtime.metrics.snapshot()}")


if __name__ == "__main__":
    main()
