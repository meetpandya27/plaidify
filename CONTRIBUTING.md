# Contributing to Plaidify

We appreciate your interest in contributing to Plaidify! Below are some guidelines to help you get started.

---

## Getting Started

1. Fork the repository on GitHub and clone your forked repo locally.
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. (Optional) Run the project locally:
   ```bash
   uvicorn src.main:app --reload
   ```
   Then visit http://127.0.0.1:8000/docs to check the local API docs.

---

## Coding Style

- We recommend following Python’s [PEP8](https://www.python.org/dev/peps/pep-0008/) guidelines.
- Use type hints where relevant to improve code clarity and maintainability.
- Avoid logging of sensitive credentials or PII (personally identifiable information).

---

## Tests

- We use Python’s built-in `unittest` or `pytest` (as appropriate) for unit/integration tests. Test files live in the `tests/` directory.
- Ensure that your proposed changes do not break existing tests. Add or update tests if you introduce new functionalities or modify existing code.
- You can run the test suite with:
  ```bash
  pytest
  ```
  or
  ```bash
  python -m unittest discover tests
  ```

---

## Pull Requests

1. Create a new branch for each feature or bug fix.
2. Make your changes with clear, concise commits.
3. Include or update tests for any changes.
4. Open a Pull Request (PR) and fill out the provided template, detailing your changes and any dependencies.
5. Wait for maintainers to review your PR. We may suggest improvements or ask for clarifications.

---

## Good First Issues

If you’re new to the codebase, look for issues labeled **“good first issue”** to get started. These typically require minimal context and help you familiarize yourself with the project.

---

## Security

- Don’t log or expose any user credentials.
- Ensure ephemeral session handling when dealing with live site credentials.
- Keep an eye out for vulnerabilities in dependencies or newly introduced APIs.

---

## Community

Feel free to reach out via GitHub issues for any questions or clarifications. We love engaging with the community to keep Plaidify robust, secure, and user-friendly.

Thank you for your interest in contributing, and happy coding!