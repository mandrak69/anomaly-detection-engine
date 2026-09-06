from anomaly_detection_engine.config import load_config
from anomaly_detection_engine.observability.logging_config import configure_logging
from anomaly_detection_engine.pipeline import run_analysis, run_ingestion
from anomaly_detection_engine.runtime import build_runtime


def main() -> None:
    configure_logging()

    config = load_config()
    runtime = build_runtime(config)

    events = run_ingestion(runtime, config)
    run_analysis(runtime, events, config)


if __name__ == "__main__":
    main()
