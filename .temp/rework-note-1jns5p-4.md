# Round-4 rework: EXECUTE THESE EXACT COMMANDS

Your previous run handed off with checklist 9/9 while applying NONE of
the verdict-3 findings. Do not paraphrase; run these commands from
/Users/iv/Developer/Wildberries/cocoaskills, then verify each with the
grep shown, then paste all outputs into the outcome resource.

1. V1 (wrong version attribution):
   perl -pi -e 's/записан версией csk <=0\.9 в микросекундном формате/записан версией csk до 0.12.0 включительно в микросекундном формате; начиная с 0.12.1 csk пишет метку с точностью до секунды/' docs/troubleshooting.md
   Verify: grep -n "0.12.1" docs/troubleshooting.md   (must return line 7)

2. V2a (symlink term):
   perl -pi -e 's/поверх символических ссылок/поверх symlink/' docs/troubleshooting.md
   Verify: grep -c "символическ" docs/troubleshooting.md   (must print 0)

3. V2b (refs term):
   perl -pi -e 's/работает по локальным ссылкам/работает по локальным refs/' docs/troubleshooting.md
   Verify: grep -n "локальным refs" docs/troubleshooting.md   (must return line 47)

4. V3: read TASK-260822-1jns5p_rework-instructions-3.md finding V3 and
   apply its prescribed fix to the lead-in of symptom 5 (the sentence
   before the Cannot resolve tag remedy); it must not invent concepts
   csk does not have. Quote the final sentence in the outcome.

Then hand off. If any verification fails, fix before handoff, never
hand off with an unapplied finding.
