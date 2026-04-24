/**
 * Placeholder shell for the React rewrite of the Plaidify hosted Link
 * page. The real state machine, picker, credential, MFA, and success
 * screens land in #51b and beyond. This component exists so #51a can
 * deliver a clean, buildable scaffold without disturbing the legacy
 * /link experience, which remains the default until #51d flips the
 * switch.
 */
export function App() {
  return (
    <main role="main" aria-label="Plaidify Link">
      <h1>Plaidify Link</h1>
      <p>
        The React/Vite rewrite of the hosted Link page is under
        construction. The legacy page continues to serve production
        traffic until the rewrite is feature-complete.
      </p>
    </main>
  );
}
