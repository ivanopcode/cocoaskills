# Technical Prose Style Guide, Extracted from *The Go Programming Language* (Donovan & Kernighan, 2015)

Source: preface (pp. xi–xvii), ch. 1 Tutorial (pp. 1–8), §5.4 Errors (pp. 127–132),
ch. 7 Interfaces (pp. 171–176), ch. 8 Goroutines and Channels (pp. 217–225).
This is a guide to the *prose*, not to Go. All quotes are verbatim from the book.

---

## 1. Sentence structure

**DO open with a short, flat declarative and let elaboration follow.** Load-bearing claims
are often under eight words; the sentences that unpack them run longer.
> "Go is a compiled language. The Go toolchain converts a source program and the things it depends on into instructions in the native machine language of a computer." (p. 2)
> "Package main is special. It defines a standalone executable program, not a library." (p. 2)

**DO write in the active voice, and make the software the actor.** Functions, tools, and
programs do things; they are the grammatical subjects.
> "If the call to http.Get fails, findLinks returns the HTTP error to the caller without further ado." (p. 129)
> "The gofmt tool rewrites code into the standard format." (p. 3)

**DO use "we" for the shared author-and-reader journey, "you" for the reader's obligations
and choices.** Never "the user" for the reader.
> "We'll start with the now-traditional 'hello, world' example." (p. 1)
> "You must import exactly the packages you need." (p. 3)

**DO use "Let's" to shift from explanation to action.**
> "Let's now talk about the program itself." (p. 2); "Let's test this out using a new type." (p. 173)

**DON'T ban the passive — reserve it for rules where the agent is irrelevant or universal.**
> "Parentheses are never used around the three components of a for loop." (p. 6)
> "For this reason, bare returns are best used sparingly." (p. 127)

**DON'T let long sentences branch.** Long sentences in the book are additive chains
(clauses joined by commas, semicolons, "and", "so"), not nested subordination.
> "Go does not require semicolons at the ends of statements or declarations, except where two or more appear on the same line." (p. 3)

## 2. Paragraph discipline

**DO open every paragraph with its claim; evidence and example follow.** The first sentence
alone should summarize the paragraph.
> "Go takes a strong stance on code formatting." (p. 3) — then gofmt, then why.
> "Some functions always succeed at their task." (p. 127) — then examples, then the caveat.

**DO give the definition before the consequences.** A concept is named, defined in one or
two sentences, and only then used.
> "In Go, each concurrently executing activity is called a *goroutine*." (p. 217)
> "A channel is a communication mechanism that lets one goroutine send values to another goroutine." (p. 225)

**DO keep one concept per paragraph, and one paragraph per code block.** Code is introduced
by a motivating sentence ending in a colon, and followed by a paragraph that interprets it.
> "Our first example is a sequential clock server that writes the current time to the client once per second:" (p. 219)

**DO motivate before you show.** State the problem or the desire, then present the code as
its answer.
> "it runs for an appreciable time, during which we'd like to provide the user with a visual indication that the program is still running, by displaying an animated textual 'spinner.'" (p. 218)

**DO allow a paragraph to end on the "so what".** Interpretation sentences after examples
tell the reader what the example proved.
> "Notice how the program is expressed as the composition of two autonomous activities, spinning and Fibonacci computation." (p. 219)

## 3. Terminology handling

**DO italicize a term exactly once — at the moment of definition — then use it in plain
type forever after.**
> "It has automatic memory management or *garbage collection*." (p. xi)
> "This freedom to substitute one type for another that satisfies the same interface is called *substitutability*, and is a hallmark of object-oriented programming." (p. 173)

**DO defer full treatment explicitly when a term must appear early.** Give a working
approximation and a promise, never a hand-wave.
> "Slices are a fundamental notion in Go, and we'll talk a lot more about them soon. For now, think of a slice as a dynamically sized sequence s of array elements..." (p. 4)

**DO repeat the exact term instead of elegant variation.** io.Writer stays io.Writer;
"the error" stays "the error". Pronouns are used only when the referent is in the same or
previous sentence.

**DON'T define two terms in one breath.** Paired concepts get paired, parallel sentences:
> "A Reader represents any type from which you can read bytes, and a Closer is any value that you can close, such as a file or a network connection." (p. 174)

## 4. Punctuation and typography

**DO use the em-dash for inline enumeration and appositive glosses** — roughly one per
page, never for drama.
> "the time.Date function always constructs a time.Time from its components—year, month, and so on—unless the last argument (the time zone) is nil" (p. 127)
> "Go runs on Unix-like systems—Linux, FreeBSD, OpenBSD, Mac OS X—and on Plan 9 and Microsoft Windows." (p. xi)

**DO use semicolons to bind two halves of one thought** — very frequent, often several per
page. The second clause explains, contrasts, or completes the first.
> "Println is one of the basic output functions in fmt; it prints one or more values, separated by spaces..." (p. 2)
> "The second client must wait until the first client is finished because the server is *sequential*; it deals with only one client at a time." (p. 221)

**DO end the sentence before a code block with a colon.** This is the universal splice
between prose and example.

**DO use parentheses for asides, meta-notes, and cross-references** — one thought, one
sentence max, often self-deprecating or practical.
> "(We will use $ as the command prompt throughout the book.)" (p. 2)
> "(This problem is by no means unique to Google.)" (p. xiii)

**DO use quotation marks for borrowed or informal coinages, not for emphasis.**
> "often described as coming with 'batteries included'" (p. xiv); "isolated pockets of 'untyped' programming" (p. xiv)

## 5. Lists vs prose

**DON'T use bulleted or numbered lists in expository text.** Across all sampled pages there
is not a single bullet list. Everything is running prose.

**DO handle enumerations with announced count + ordinal paragraph openers.** Announce how
many items are coming, then give each its own paragraph starting with the ordinal.
> "Depending on the situation, there may be a number of possibilities. Let's take a look at five of them." (p. 128), then: "First, and most common, is to *propagate* the error..." (p. 129); "Third, if progress is impossible, the caller can print the error and stop the program gracefully..." (p. 130); "And fifth and finally, in rare cases we can safely ignore an error entirely:" (p. 131)

**DO fold short enumerations into a single sentence with commas or em-dashes.**
> "Even traditional batch problems—read some data, compute, write some output—use concurrency to hide the latency of I/O operations." (p. 217)

## 6. Tone

**DO make plain, unhedged claims when the fact is certain, and hedge precisely when it is
not.** The stock hedges are "for the most part", "usually", "generally", "typically",
"in practice", "may" — each carries real information about frequency.
> "for the most part, the order of declarations does not matter" (p. 3)
> "Usually when a function returns a non-nil error, its other results are undefined and should be ignored. However, a few functions may return partial results in error cases." (p. 128)

**DO state trade-offs and admit costs; never sell.** Zero marketing language in ~48 pages.
Limitations are stated as flatly as features.
> "This style undeniably demands that more attention be paid to error-handling logic, but that is precisely the point." (p. 128)
> "In functions like this one, ... bare returns can reduce code duplication, but they rarely make code easier to understand." (p. 127)

**DO permit judgment words when they carry engineering content**, not enthusiasm:
"terribly inefficient recursive algorithm" (p. 218), "the type interface{} ... is
indispensable" (p. 176), "its omission of garbage collection made concurrency too painful"
(p. xiii).

**DO use analogy sparingly but vividly, one image per concept.**
> "Like an envelope that wraps and conceals the letter it holds, an interface wraps and conceals the concrete type and value that it holds." (p. 176)
> "When it is ultimately handled by the program's main function, it should provide a clear causal chain ..., reminiscent of a NASA accident investigation: genesis: crashed: no parachute..." (p. 129)

**DO allow an occasional rhetorical question to pivot the argument — and answer it
immediately.**
> "So what does the type interface{}, which has no methods at all, tell us about the concrete types that satisfy it? That's right: nothing." (p. 176)
> "Why should you prefer one form to another?" (p. 7)

**DO give behavioral advice to the reader directly, imperative mood, once earned.**
> "Get into the habit of considering errors after every function call, and when you deliberately ignore one, document your intention clearly." (p. 131)

**DON'T oversell your own text.** Scope limits are admitted up front:
> "We certainly won't explain everything in the first chapter, but studying such programs in a new language can be an effective way to get started." (p. 1)

## 7. Transitions and cross-references

**DO open a section by linking it to the previous one in a single sentence** — old
information first, new information second.
> "The clock server used one goroutine per connection. In this section, we'll build an echo server that uses multiple goroutines per connection." (p. 222)

**DO make forward references explicit promises with a precise target** (chapter, section,
or §-number), never "as we will see later" alone.
> "We'll take a closer look at functions in Chapter 5." (p. 3)
> "In Section 7.11, we'll present a more systematic way to distinguish certain error values from others." (p. 132)

**DO make backward references with "Recall" plus the section number.**
> "Recall from Section 6.2 that for each named concrete type T, some of its methods have a receiver of type T itself whereas others require a *T pointer." (p. 176)

**DO defer honestly.** When a topic is postponed, say what is being postponed and where it
will be handled — the deferral is itself information.
> "We'll discuss the crucial concept of *concurrency safety* in the next chapter." (p. 225)

**DO end a chapter intro with a roadmap in prose** (not a list):
> "In this chapter, we'll start by looking at the basic mechanics of interface types and their values. Along the way, we'll study several important interfaces from the standard library. ... Finally, we'll look at *type assertions* (§7.10) and *type switches* (§7.13)..." (p. 171)

---

## Summary card

| Dimension | Rule of thumb |
|---|---|
| Sentences | Short claim first; long sentences additive, not nested; active voice; software as subject |
| Voice | "we" = authors+reader, "you" = reader's duty, "Let's" = start doing |
| Paragraphs | Claim → elaboration → example → interpretation; one concept each |
| Terms | Italic once at definition, exact repetition after; approximate now + promise details later |
| Punctuation | Semicolon binds twin clauses; em-dash glosses inline; colon precedes every code block |
| Lists | None; announced count ("five of them") + "First / Third / And fifth and finally" paragraphs |
| Tone | No marketing; costs stated flatly; calibrated hedges ("usually", "in practice"); rare vivid analogy |
| References | "Recall from §X"; "We'll see ... in §Y"; sections open by bridging from the previous one |
