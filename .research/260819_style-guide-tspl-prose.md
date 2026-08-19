# TSPL Prose Style Guide

A technical-writing style guide extracted from "The Swift Programming Language" (TSPL).
Source: epub extracted to `.temp/swift_book/`; sampled chapters: About Swift, A Swift Tour,
The Basics, Protocols, Concurrency, Automatic Reference Counting.
All quotes are verbatim from the book.

---

## 1. Sentence structure

**DO: Write in active voice with one of three subjects — the technology, the concept, or "you".**
The subject carries the responsibility: the language does things, concepts define things, the reader performs actions.

> "Swift provides many fundamental data types, including `Int` for integers, `Double` for floating-point values..."

> "You declare constants with the `let` keyword and variables with the `var` keyword."

**DO: Lead with the goal, then the mechanism — "To do X, you write Y."**
Goal-first infinitive clauses tell the reader why before how.

> "To indicate that a function or method is asynchronous, you write the `async` keyword in its declaration after its parameters..."

> "To call an asynchronous function and let it run in parallel with code around it, write `async` in front of `let` when you define a constant..."

**DO: Keep sentences to one idea; use a second sentence rather than stacking clauses.**
Typical sentence length is 15–25 words. Compound sentences exist, but each clause states one fact.

> "The value of a constant can't be changed once it's set, whereas a variable can be set to a different value in the future."

**DO: Use contractions.** The book consistently writes "don't", "can't", "it's", "you're". Formal non-contracted forms would read stiffer than the source.

**DON'T: Use passive voice to hide the actor.** Passive appears only when the actor is genuinely irrelevant ("Comments are ignored by the Swift compiler"), never as a default.

---

## 2. Paragraph discipline

**DO: Open with the definition or the claim; consequences and details follow.**
The first sentence of a section defines the thing. Everything after elaborates.

> "A protocol defines a blueprint of methods, properties, and other requirements that suit a particular task or piece of functionality. The protocol can then be adopted by a class, structure, or enumeration..."

> "Integers are whole numbers with no fractional component, such as `42` and `-23`."

**DO: Introduce one new concept per paragraph.** In The Basics intro, each paragraph owns exactly one idea: types, then variables/constants, then tuples, then optionals, then safety — never two at once.

**DO: Introduce code with a lead-in sentence ending in a colon, then explain the code after it.**
The lead-in says what the example shows; the follow-up paragraph walks through what happened, using "This example..." / "In this example...".

> "Here's an example of a simple structure that adopts and conforms to the `FullyNamed` protocol:"

> (after the code) "This example defines a structure called `Person`, which represents a specific named person."

**DO: State a drawback before offering the fix.** Problem first, solution second, in that order.

> "This approach has an important drawback: Although the download is asynchronous... only one call to `downloadPhoto(named:)` runs at a time. ... However, there's no need for these operations to wait..."

**DON'T: Drop code on the reader without a lead-in, and don't leave code unexplained.** Every non-trivial listing in the book is bracketed by a setup sentence and a walkthrough.

---

## 3. Terminology

**DO: Define a term at first use with "known as" / "called", then use it consistently.**

> "Swift also makes extensive use of variables whose values can't be changed. These are known as constants..."

> "...multiple pieces of code try to access some piece of shared mutable state—this is known as a data race."

**DO: Repeat the term instead of using a pronoun.** Names are repeated in full, even when a pronoun would be shorter — "the `welcomeMessage` variable", "the `FullyNamed` protocol", again and again. "It" only refers back within the same sentence or to an unambiguous antecedent.

> "The `welcomeMessage` variable can now be set to any string value without error:"

**DO: Use full API signatures every time, not shortened names.**

> "You can print the current value of a constant or variable with the `print(_:separator:terminator:)` function:"

**DO: Announce deliberate word choices for the rest of the text.**

> "The rest of this chapter uses the term concurrency to refer to this common combination of asynchronous and parallel code."

**DON'T: Rotate synonyms for elegance.** A protocol is always "adopted" and "conformed to", never "implemented/supported/used" interchangeably. One term, one meaning.

---

## 4. Punctuation and typography

**DO: Use an unspaced em-dash to bolt an example or clarification onto a claim.**
This is the book's signature move: claim, dash, concrete illustration or sharpening.

> "The integer types include their size and sign in their names—for example, an 8-bit unsigned integer is of type `UInt8`..."

> "The protocol doesn't specify whether the property should be a stored property or a computed property—it only specifies the required property name and type."

**DO: Use a colon to introduce code, formal readings, and expansions.**

> "This code can be read as: 'Declare a new constant called `maximumNumberOfLoginAttempts`, and give it a value of `10`.'"

**DO: Use parentheses for low-priority asides — concrete name examples, "that is" glosses, and edge-case reassurance.**

> "Constants and variables associate a name (such as `maximumNumberOfLoginAttempts` or `welcomeMessage`) with a value of a particular type (such as the number `10` or the string `"Hello"`)."

> "For instance methods on value types (that is, structures and enumerations) you place the `mutating` keyword before a method's `func` keyword..."

**DO: Mark every inline code element — keywords, type names, literals, values — as code.** `let`, `Int`, `42`, `"Hello"` are always set in code style, never plain text.

**DON'T: Use exclamation marks, scare quotes, or ellipses for drama.** Quotation marks appear only for coined readings ("of type `String`" means "can store any `String` value") and named strings.

---

## 5. Lists vs prose

**DO: Break into a bulleted list when enumerating three or more parallel facts, options, or steps.**
Bullets are grammatically parallel and each is a complete statement.

> "Integer literals can be written as: A decimal number, with no prefix / A binary number, with a `0b` prefix / An octal number, with a `0o` prefix / A hexadecimal number, with a `0x` prefix"

> "Call asynchronous functions with `await` when the code on the following lines depends on that function's result. This creates work that is carried out sequentially." (one bullet of a decision list)

**DO: Keep short enumerations (2–4 items) inline with commas.**

> "Swift is a fantastic way to write software for phones, tablets, desktops, servers, or anything else that runs code."

**DO: Use numbered/sequenced bullets for execution walkthroughs.** The Concurrency chapter narrates "one possible order of execution" as a step list: "The code starts running from the first line and runs up to the first `await`..."

**DON'T: Use a list as a substitute for explanation.** Lists carry parallel facts; reasoning stays in prose around them.

---

## 6. Tone

**DO: State facts flatly; save enthusiasm for the marketing preface only.** "About Swift" is the sole hype zone ("Swift is a fantastic way to write software..."). Guide chapters never evaluate; they describe and instruct.

**DO: Give direct prescriptions with "always" / "only", followed immediately by the rationale.**

> "Unless you need to work with a specific size of integer, always use `Int` for integer values in your code. This aids code consistency and interoperability."

> "If a stored value in your code won't change, always declare it as a constant with the `let` keyword. Use variables only for storing values that change."

**DO: State platform and capability limitations plainly, paired with the safe default.**

> "Some floating-point types are supported only by certain platforms, but `Float` and `Double` are available on all platforms."

> "There's no way to take a bottom-up approach, because synchronous code can't ever call asynchronous code."

**DO: Introduce caveats with "However," and put them in their own sentence or Note block.** Notes hold the second-order caveats — edge cases, rare needs, cross-references — keeping the main flow clean.

> "Note — It's rare that you need to write type annotations in practice."

**DO: Acknowledge cost and complexity honestly.**

> "The additional scheduling flexibility from parallel or asynchronous code also comes with a cost of increased complexity."

**DON'T: Blame the reader or hedge.** Errors are framed as things the language catches, not reader failures: "type safety prevents you from passing it an `Int` by mistake."

---

## 7. Reference-manual patterns

**DO: Use the "X is Y. Use X to Z." skeleton for introducing constructs.**
Definition sentence, then an imperative usage sentence.

> "Use comments to include nonexecutable text in your code, as a note or reminder to yourself. Comments are ignored by the Swift compiler when your code is compiled."

> "Use `let` to make a constant and `var` to make a variable."

**DO: Scope negative constraints precisely — say what something does NOT do or require.**

> "The `RandomNumberGenerator` protocol doesn't make any assumptions about how each random number will be generated—it simply requires the generator to provide a standard way..."

**DO: Teach new syntax by analogy to syntax the reader already knows.**

> "You write `await` in front of the call to mark the possible suspension point. This is like writing `try` when calling a throwing function..."

**DO: Defer depth with explicit cross-references — "as described in X" / "For information about X, see Y."**

> "...matches integer type inference, as described in Type Safety and Type Inference."

> "For information about parameters with default values, see Default Parameter Values."

**DO: End a syntax rule with its precise placement.** Rules name the exact position of a token: "write the `async` keyword in its declaration after its parameters", "before the return arrow (`->`)".

**DON'T: Preview everything up front.** Each chapter states its scope in one intro paragraph, then trusts cross-references for anything out of scope: "Don't worry if you don't understand something—everything introduced in this tour is explained in detail in the rest of this book."

---

## Quick checklist

- Subject is Swift / the concept / "you"; active voice; contractions on.
- Definition first, one concept per paragraph, problem before solution.
- Code: colon lead-in, listing, "This example..." walkthrough.
- Terms defined once ("known as X"), then repeated verbatim — no synonym rotation, minimal pronouns.
- Em-dash for bolted-on examples; parentheses for asides; every identifier in code style.
- Bullets for 3+ parallel items; prose for reasoning.
- Prescriptions use "always/only" plus a rationale; caveats live in "However," sentences and Note blocks.
- No hype, no hedging, no blame; limitations stated flatly with the safe default alongside.
