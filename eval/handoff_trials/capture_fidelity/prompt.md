Please do all of this in order, then stop:
1. Run the test suite (`python -m pytest -q`) and tell me what fails.
2. Fix the EU amount parsing in invoicer/parse.py so "1.234,50" parses as 1234.50 (edit the file).
3. Run `python tools/gen.py` to regenerate the rates table.
4. Use a subagent (the Task tool) to list every TODO comment in the repository.
5. Use the release-notes skill to write the release notes.
6. Create a ticket with the tickets MCP tool titled "VAT rounding per line" for the work that is left, and tell me its id.
