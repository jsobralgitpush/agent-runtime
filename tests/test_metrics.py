from urllib.request import urlopen

from app.metrics import start_worker_metrics_server


def test_worker_metrics_server_exposes_process_registry() -> None:
    server, thread = start_worker_metrics_server("127.0.0.1", 0)
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/metrics", timeout=1) as response:
            body = response.read().decode()

        assert response.status == 200
        assert "agent_runtime_worker_claims_total" in body
        assert "agent_runtime_workflow_executions_total" in body
        assert "agent_runtime_queue_depth" in body
        assert "agent_runtime_queue_oldest_pending_age_seconds" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
