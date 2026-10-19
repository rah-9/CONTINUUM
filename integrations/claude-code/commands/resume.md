---
description: Continue the work in this repository from Continuum's brief (after switching model, chat or app)
---

Call `coding_resume(repo_path=<this repository>, agent=<your exact model id>)` and read the
brief. "What the user asked" holds the user's own words from earlier chats: they are the
requirements, and the higher-numbered message wins a conflict. Start from the files under
"Code touched in this session", not by exploring the repository. Then continue with the
latest instruction. Use `coding_recall(query)` for anything older than the brief shows.
