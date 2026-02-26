"""FizzBuzz via composable cycles.

Instead of branching on modulo checks, this models Fizz and Buzz as two
independent periodic signals — empty strings on off-beats, labels on
downbeats. Concatenating the two signals each tick produces the correct
composite label, and Python's falsy empty string lets the number fall
through when neither signal fires.
"""

from itertools import cycle


def fizzbuzz(n: int) -> list[str]:
    """Return FizzBuzz labels for 1..n."""
    fizz = cycle(["", "", "Fizz"])
    buzz = cycle(["", "", "", "", "Buzz"])
    return [next(fizz) + next(buzz) or str(i) for i in range(1, n + 1)]


if __name__ == "__main__":
    print("\n".join(fizzbuzz(100)))
