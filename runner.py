"""Backward-compatible root entry point for the maintained Phase 2 worker."""

from worker.runner import main, process_job


if __name__ == "__main__":
    main()
