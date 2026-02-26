"""FizzBuzz with data-driven rules."""


def fizzbuzz(limit: int) -> list[str]:
    """Build FizzBuzz labels from 1 to ``limit``.

    Args:
        limit: The inclusive upper bound.

    Returns:
        A list where multiples of 3 become "Fizz", multiples of 5 become
        "Buzz", and multiples of both become "FizzBuzz".
    """
    if limit < 1:
        return []

    rules: tuple[tuple[int, str], ...] = ((3, "Fizz"), (5, "Buzz"))
    labels: list[str] = []

    for number in range(1, limit + 1):
        word = "".join(text for divisor, text in rules if number % divisor == 0)
        labels.append(word if word else str(number))

    return labels


if __name__ == "__main__":
    print("\n".join(fizzbuzz(100)))
