# Local screenshot evidence

These are unaltered browser screenshots captured from the running local Compose
stack on November 15, 2026 (America/Los_Angeles). They show actual application
responses and Grafana data, rather than mockups. No AI-generated imagery or
retouching was used. Each image was visually inspected for credentials and
unrelated records before inclusion.

| Image | What it shows |
| --- | --- |
| [swagger-ui.png](swagger-ui.png) | The live FastAPI Swagger UI and available API operations. |
| [completed-job.png](completed-job.png) | An actual Swagger GET response for a dedicated `portfolio-success-demo` job: `sum_numbers`, input `[10,20,30]`, `completed`, one attempt, result `total: 60`, and no error. |
| [failed-job.png](failed-job.png) | An actual Swagger GET response for a dedicated `portfolio-failure-demo` job: `simulate_failure`, `failed`, three attempts, and the intentional failure message. A fourth sequential retry was separately verified to return HTTP 409. |
| [grafana-metrics.png](grafana-metrics.png) | Grafana Explore with live API submission counters grouped by priority over the preceding 15 minutes. This is an Explore graph, not a saved dashboard or worker-execution metric. |

The Grafana query is:

```promql
sum by (priority) (queueops_jobs_submitted_total{job="queueops-api",instance="api:8000"})
```

Two dedicated demo job records were created and retained for the job screenshots.
No existing job list was captured. Grafana displays aggregate API metrics from
the running local service; the counter does not measure execution success or
failure. Login credentials, tokens and cookies are absent from these assets.
No paid provider requests were made.
