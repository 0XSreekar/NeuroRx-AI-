# Contributing to NeuroRx-AI

NeuroRx-AI is a clinical decision support prototype. Contributions are welcome, and clinical safety comes before features.

## Ground rules

This project is research software and is not a medical device. It does not diagnose patients and its output must never be used as a substitute for a qualified clinician. Any contribution that presents model output as a definitive diagnosis or removes an existing safety disclaimer will be closed.

Never commit patient data. Use synthetic or properly de-identified records only, and keep real lab reports and imaging out of the repository and out of issue attachments.

## Getting started

Fork the repository, clone your fork, create a virtual environment, and install the dependencies listed in the README. Copy .env.example to .env and fill in your own local keys. Work on a branch named after the change, for example feat/lab-panel-parser.

## Making a change

Keep each pull request to a single concern and describe the clinical reasoning behind it where relevant. Cite a guideline or source when you change how a recommendation is generated. Add or update tests under tests for any change to the reasoning pipeline, and keep prompt changes reviewable by explaining what behaviour you expect to shift.

## Reporting issues

Include the steps to reproduce, the input you used, what you expected, and what happened instead, along with your Python and OS versions. For an incorrect or unsafe recommendation, describe the case in general terms rather than pasting a real patient record.
