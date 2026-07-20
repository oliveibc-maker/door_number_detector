"""
Script de setup para instalação do pacote
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="door_number_detector",
    version="1.0.0",
    author="oliveibc-maker",
    description="Detecção de números de portas via Google Street View com OCR",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/oliveibc-maker/door_number_detector",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.10",
    install_requires=[
        "requests>=2.31.0",
        "paddleocr==2.7.3",
        "paddlepaddle==2.6.2",
        "Pillow>=10.0.0",
        "opencv-python>=4.6.0",
        "numpy>=1.26.0",
        "SQLAlchemy>=2.0.20",
        "python-dotenv>=1.0.0",
        "pandas>=2.0.0",
        "pyodbc>=5.0.0",
        "openpyxl>=3.1.0",
    ],
)
