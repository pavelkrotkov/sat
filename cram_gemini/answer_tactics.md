# SAT Tactic Upgrade: The 1-Week Fix

**Objective:** Shift from "solving" to "mechanically dismantling" high-value trap questions.
**Context:** You are losing points not because of a lack of knowledge, but because of specific, repeatable traps. These tactics are designed to be rigid procedures to bypass those traps.

---

## 📐 Math Tactics

### Tactic 1: The "No Real Solutions" Trigger
**The Trap:** You see a quadratic equation and the phrase "no real solutions," and you try to solve for $x$ or guess values.
**The Fix:** STOP. Do not solve for $x$.
1.  **Identify:** "No real solutions" = **Discriminant is Negative**.
2.  **Formula:** Write $b^2 - 4ac < 0$ immediately.
3.  **Execute:** Plug in $a, b, c$ and solve the inequality.

*   **Example (Test 4, Q13):** $x^2 - 34x + c = 0$. No real solutions.
    *   *Wrong Move:* Guessing $c$.
    *   *Right Move:*
        *   $a=1, b=-34, c=c$.
        *   $(-34)^2 - 4(1)(c) < 0$
        *   $1156 - 4c < 0 \rightarrow 1156 < 4c \rightarrow 289 < c$.
        *   Answer: 290 (or anything $> 289$).

### Tactic 2: Vertex Form Translation
**The Trap:** You see a word problem about "maximum profit," "minimum height," or "reaches a peak," and you try to model it with $y=mx+b$ or standard form.
**The Fix:** "Max/Min" = **Vertex Form**.
1.  **Template:** Write $y = a(x-h)^2 + k$.
2.  **Fill Vertex:** $(h, k)$ is the max/min point given in the text.
3.  **Find $a$:** Plug in *any other point* $(x, y)$ given in the text to solve for $a$. **Do not assume $a=1$ or $a=-1$.**

*   **Example (Test 4, Q22):** Vertex is $(9, -14)$. Passes through origin (implied or other point).
    *   *Step 1:* $y = a(x-9)^2 - 14$.
    *   *Step 2:* Plug in other point to find $a$.
    *   *Step 3:* Expand to standard form $ax^2 + bx + c$ only if requested.

### Tactic 3: Dimensional Analysis for Area/Volume
**The Trap:** You convert units linearly ($1 \text{ ft} = 12 \text{ in}$) when dealing with Area ($ft^2$) or Volume ($ft^3$).
**The Fix:** **Square the Factor.**
1.  **Identify:** Is it Area ($^2$) or Volume ($^3$)?
2.  **Modify Factor:** If $1 \text{ unit A} = k \text{ unit B}$...
    *   Area: $1 \text{ unit A}^2 = k^2 \text{ unit B}^2$.
    *   Volume: $1 \text{ unit A}^3 = k^3 \text{ unit B}^3$.

*   **Example (Test 9, Q21):** Convert $x \text{ m}^2$ to $\text{ft}^2$ where $1 \text{ m} \approx 3.28 \text{ ft}$.
    *   *Wrong Move:* Multiply by 3.28.
    *   *Right Move:* Multiply by $(3.28)^2 \approx 10.76$.

---

## 📖 Reading & Writing Tactics

### Tactic 4: The "Slash & Burn" for Grammar
**The Trap:** You read the sentence "by ear" to see where the pause falls.
**The Fix:** **Cross out the junk.**
1.  **Prepositional Phrases:** Cross out anything starting with *of, in, at, for, by, on, with*.
2.  **Appositives:** Cross out the fluff between commas (e.g., ", a famous painter,").
3.  **Check Match:** Match the remaining Subject to the Verb.

*   **Example (Test 8, Q20):** "The shape of the wings... [is/are]..."
    *   *Slash:* "The shape ~~of the wings~~..."
    *   *Result:* "The shape... **is**." (Singular).

### Tactic 5: Scientific Logic Mapping ($A \to B$)
**The Trap:** You get overwhelmed by jargon ("piezophiles," "TMAO," "Neronian tools") and pick an answer that repeats keywords but gets the logic wrong.
**The Fix:** **Simplify to Variables.**
1.  **Ignore Nouns:** Call them $X$ and $Y$.
2.  **Map the Arrow:** Does $X$ cause $Y$? Does $X$ prevent $Y$?
3.  **Identify the Gap:** The question usually asks for support. Find the choice that strengthens the arrow.

*   **Example (Test 7, Q12 - TMAO):**
    *   *Text:* Pressure compresses water ($P \to C$). TMAO is high in deep fish. Hypothesis: TMAO prevents compression ($T \to \text{No } C$).
    *   *Task:* Support the hypothesis.
    *   *Target:* We need evidence that **TMAO stops compression**.
    *   *Choice D:* "Hydrogen bonds are more stable (less compressed) when TMAO is present." $\to$ Matches $T \to \text{No } C$.

### Tactic 6: The "Prompt First" Protocol (Rhetorical Synthesis)
**The Trap:** You read the bullet points, understand the facts, and pick the answer that summarizes them best.
**The Fix:** **Read the Question Sentence FIRST.**
1.  **Ignore Bullets:** Jump straight to "The student wants to..."
2.  **Identify Constraint:** Underline the specific goal (e.g., "emphasize a contrast," "explain the origin," "introduce the study").
3.  **Eliminate:** Cross out any answer that is true but ignores the constraint.

*   **Example:** "The student wants to emphasize the **contrast** between the two theories."
    *   *Choice A:* Theory X says this, and Theory Y says that. (No transition).
    *   *Choice B:* **While** Theory X says this, Theory Y argues that. (Has "While" = Contrast).
    *   *Select:* B.

### Tactic 7: The "Full Stop" Check (Boundaries)
**The Trap:** You see a transition word like "however" or "therefore" and assume it needs a semicolon or comma based on "flow."
**The Fix:** **The Independent Clause Test.**
1.  **Test:** Can the part *before* the punctuation stand alone as a sentence? Can the part *after* stand alone?
2.  **Rule:**
    *   Yes + Yes = **Period** (.) or **Semicolon** (;).
    *   Yes + No = **Comma** (,).
    *   **CRITICAL:** "Semicolon + However + Comma" is a standard pattern for joining two full sentences. "; However,".

*   **Example (Test 7, Q17):** "...capture its prey; rather, the brightly colored arachnid..."
    *   *Check:* "capture its prey" (Sentence). "the brightly colored arachnid..." (Sentence).
    *   *Fix:* Needs a hard stop. Semicolon is the only choice that acts like a period.
