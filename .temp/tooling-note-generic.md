# Mandatory tooling note

The agy provider's native write_to_file/artifact tool CANNOT write files
into this repository (it only accepts paths inside its own brain
directory) and a previous run on this board handed off with a complete
checklist while its file edits were silently lost.

Rules for this run:

1. Edit repository files ONLY through shell commands (cat > file,
   python3 heredoc, perl -pi -e), never through your native
   write_to_file/artifact tool.
2. Work directory is /Users/iv/Developer/Wildberries/cocoaskills. Verify
   with pwd before editing.
3. After every file edit, verify with grep/head that the change is
   actually in the file, and include that verification output in your
   outcome resource.
