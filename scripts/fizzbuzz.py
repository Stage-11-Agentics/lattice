#!/usr/bin/env python3
"""FizzBuzz from 0 to 100. LAT-106 process validation."""

for n in range(101):
    if n % 15 == 0:
        print("FizzBuzz")
    elif n % 3 == 0:
        print("Fizz")
    elif n % 5 == 0:
        print("Buzz")
    else:
        print(n)
