# Rework strategy (mandatory)

Your first pass translated sections 1-3 and silently dropped or left in
English sections 4-14, losing normative rules. Do NOT rewrite from
memory. Work mechanically:

1. Recover the English original: git show HEAD:docs/skill-authoring.md > .temp/skill-authoring.orig.md
2. Process ONE numbered section at a time (4, then 5, ... then 14):
   translate the prose of that section faithfully, keep every rule and
   constraint, keep code blocks and identifiers byte-identical, then
   verify the section landed with grep before moving on.
3. Never compress: if the original states a rule, the translation states
   the same rule. Target length is close to the original, not shorter.
4. Fix every numbered finding in the attached review verdict, including
   артифакт -> артефакт everywhere, and the mistranslations at lines
   310, 345-347, 353, 387, 457.
5. Shell-only edits, quoted heredocs, pwd check, grep evidence in the
   outcome resource. Literal outputs only.
