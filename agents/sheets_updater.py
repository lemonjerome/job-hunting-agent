# Phase 5 — Sheets Updater Agent
# Finds or creates the "Job Applications" sheet in GDrive "Job Application" folder.
# Deduplicates by URL, appends new job entries.
# Outputs: state["new_jobs"] — only newly added entries.
