from setuptools import setup, find_packages

setup(
    name="projecthephaestus",
    version="0.1.0",
    author="HomericIntelligence Team",
    author_email="team@homericintelligence.com",
    description="Shared utilities and tooling for the HomericIntelligence ecosystem",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/HomericIntelligence/ProjectHephaestus",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.8",
    install_requires=[
        # Currently no dependencies, but we might add them later
    ],
    extras_require={
        "dev": [
            "pytest>=6.0.0",
            "black>=21.0.0",
            "flake8>=3.8.0",
            "mypy>=0.800",
        ],
    },
    entry_points={
        "console_scripts": [
            # We can add CLI tools here when we have them
        ],
    },
)
