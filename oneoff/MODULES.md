# Module ownership and shared contracts

The six feature modules have exclusive ownership of their module file and matching test file. Their paths and test names follow the parallelization plan; existing `oneoff/analysis/*` code and its existing tests remain read-only.

| Module | File(s) | Test file(s) | Imported contracts from `oneoff.contracts` |
|---|---|---|---|
| config | `oneoff/config.py` | `oneoff/tests/test_config.py` | `AppConfig`, `RunPolicy` |
| inventory | `oneoff/inventory.py` | `oneoff/tests/test_inventory.py` | `AppConfig`, `RunPolicy`, `Candidate`, `SkippedClip`, `Selection`, `ClipAnalysis`, `ClipCollector` |
| diagnosis | `oneoff/diagnosis.py` | `oneoff/tests/test_diagnosis.py` | `ClipAnalysis`, `Diagnosis` |
| repair | `oneoff/repair.py` | `oneoff/tests/test_repair.py` | `ClipAnalysis`, `Diagnosis`, `RepairArtifact`, `FfmpegRunner`, `CommandResult` |
| publication | `oneoff/publication.py` | `oneoff/tests/test_publication.py` | `ClipAnalysis`, `Diagnosis`, `RepairArtifact`, `Verification`, `PublicationResult`, `ClipCollector`, `AudioFingerprinter`, `AudioFingerprint`, `NoReplaceRename` |
| run_report | `oneoff/run_report.py` | `oneoff/tests/test_run_report.py` | `AppConfig`, `RunReport`, `ReportEvent`, `ClipOutcome`, `RunSummary`, `Candidate` |

The integrator separately owns `oneoff/one-off_transcode.py` and `oneoff/tests/test_workflow.py`; the plan does not prescribe that entry point's exact import list. Shared definitions are centralized in `oneoff/contracts.py` and must stay free of imports from the six feature modules.
