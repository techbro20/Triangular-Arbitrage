"""
Event-driven cyclic (triangular+) arbitrage bot — paper trading first.

Native WebSocket order books drive cycle checks on MEXC and KuCoin.
Opportunities are validated with fee-aware depth walks. Live trading is
disabled until you explicitly opt in later.
"""

from setuptools import find_packages, setup

from triangular_arbitrage import PROJECT_NAME, VERSION

with open("README.md", encoding="utf-8") as f:
    DESCRIPTION = f.read()

REQUIRED = [
    line.strip()
    for line in open("requirements.txt", encoding="utf-8")
    if line.strip() and not line.startswith("#")
]

setup(
    name=PROJECT_NAME.lower().replace("-", "_"),
    version=VERSION,
    description="Paper-first cyclic arbitrage on event-driven order books",
    packages=find_packages(exclude=["tests"]),
    long_description=DESCRIPTION,
    long_description_content_type="text/markdown",
    tests_require=["pytest", "pytest-asyncio"],
    test_suite="tests",
    zip_safe=False,
    install_requires=REQUIRED,
    python_requires=">=3.11",
    entry_points={
        "console_scripts": [
            "triangular-arb=triangular_arbitrage.app:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
