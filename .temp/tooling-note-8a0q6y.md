# Mandatory tooling note for this rework

Your previous run FAILED to apply the fixes: it handed off with checklist
12/12 while README.md line 108 still contains the nonexistent command
`csk install --global`. The run log shows your native write_to_file tool
rejected the absolute repo path (artifacts must live in the agy brain
directory), and no fallback was attempted. Do not trust your previous
run's claims.

Rules for this run:

1. Edit repository files ONLY through shell commands (python3 heredoc,
   perl -pi -e, or cat > file), never through your native
   write_to_file/artifact tool. Verify every edit with grep immediately
   after writing.
2. Work directory is /Users/iv/Developer/Wildberries/cocoaskills. Verify
   with pwd before editing.
3. Apply EVERY blocking finding from the attached reviewer verdict
   (TASK-260819-8a0q6y_rework-instructions.md), then re-verify each one
   with grep and paste the grep output into your outcome resource.
4. Before handing off, run: grep -n "csk install --global" README.md
   and confirm it returns nothing.
