package com.ttc.dcinv.keycloak;

import jakarta.ws.rs.core.MultivaluedMap;
import jakarta.ws.rs.core.Response;

import org.jboss.logging.Logger;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.events.Errors;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.services.managers.BruteForceProtector;

/**
 * Custom Authenticator step: validates a 4-digit numeric PIN from the user's
 * {@code mobile_pin} attribute.
 *
 * <p>Designed to sit immediately after the standard {@code Username Form} step
 * in a Browser flow. The username step populates {@code context.getUser()}; we
 * read the PIN from the form and compare it to the user's {@code mobile_pin}
 * attribute in constant time.
 *
 * <p>Failures are routed through Keycloak's built-in {@link BruteForceProtector}
 * so the realm's "max failed attempts" / "wait window" settings apply to PIN
 * attempts exactly as they would to password attempts.
 *
 * <p>The error message is generic regardless of cause (PIN wrong, attribute
 * missing, attribute empty) — we deliberately don't leak whether the username
 * has mobile access at all.
 *
 * <p>WHAT THIS DOES NOT DO:
 * <ul>
 *   <li>Hash the PIN — stored as plain text in the user attribute. The
 *       Keycloak DB is behind VPN; 4-digit entropy makes hashing largely
 *       cosmetic. Upgrade to PBKDF2 if/when threat model changes.</li>
 *   <li>Validate PIN format on the server side — we trust the {@code <input
 *       pattern>} on the form. Adding a server-side digit-only check is a
 *       one-liner if/when needed.</li>
 *   <li>Surface a "set PIN" page — provisioning is admin-driven via the
 *       Keycloak Admin Console. See docs/keycloak-pin-flow.md.</li>
 * </ul>
 */
public class MobilePinAuthenticator implements Authenticator {

    private static final Logger LOG = Logger.getLogger(MobilePinAuthenticator.class);

    /** User attribute storing the per-user PIN. Single value, plain digits. */
    public static final String PIN_ATTRIBUTE = "mobile_pin";

    /** Form field name on the FreeMarker template. */
    public static final String PIN_FORM_FIELD = "pin";

    /** Theme template file. Must be placed in the active login theme. */
    public static final String PIN_TEMPLATE = "login-mobile-pin.ftl";

    /** Message key resolved by the theme's messages bundle. */
    public static final String ERROR_MESSAGE_KEY = "invalidPinMessage";

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        // Initial render — user has been set by the preceding Username Form step,
        // we just paint the PIN entry page and yield control back to Keycloak.
        Response challenge = buildChallenge(context, null);
        context.challenge(challenge);
    }

    @Override
    public void action(AuthenticationFlowContext context) {
        UserModel user = context.getUser();
        if (user == null) {
            // Should never happen with REQUIRED Username Form before us. Fail safe.
            LOG.warn("mobile_pin action invoked with no user in context");
            context.failure(AuthenticationFlowError.INTERNAL_ERROR);
            return;
        }

        if (!user.isEnabled()) {
            LOG.debugf("mobile_pin: user %s is disabled, refusing", user.getUsername());
            context.failure(AuthenticationFlowError.USER_DISABLED);
            return;
        }

        RealmModel realm = context.getRealm();
        KeycloakSession session = context.getSession();
        BruteForceProtector bruteForce = session.getProvider(BruteForceProtector.class);
        boolean bruteForceEnabled = bruteForce != null && realm.isBruteForceProtected();

        // Pre-check: if Keycloak has already locked this user (e.g. too many
        // prior PIN attempts in the window), don't even compare the input.
        if (bruteForceEnabled && bruteForce.isTemporarilyDisabled(session, realm, user)) {
            context.getEvent().user(user).error(Errors.USER_TEMPORARILY_DISABLED);
            context.failure(AuthenticationFlowError.USER_TEMPORARILY_DISABLED);
            return;
        }

        MultivaluedMap<String, String> form = context.getHttpRequest().getDecodedFormParameters();
        String pinInput = form.getFirst(PIN_FORM_FIELD);

        if (pinInput == null || pinInput.isEmpty()) {
            // Empty submission — treat as a failed attempt for brute-force accounting,
            // otherwise an attacker could probe the page repeatedly without consequence.
            recordFailedAttempt(context, user, bruteForce, bruteForceEnabled);
            return;
        }

        String storedPin = user.getFirstAttribute(PIN_ATTRIBUTE);
        boolean valid = storedPin != null
                && !storedPin.isEmpty()
                && constantTimeEquals(storedPin, pinInput);

        if (!valid) {
            recordFailedAttempt(context, user, bruteForce, bruteForceEnabled);
            return;
        }

        // Success — clear any pending brute-force counter and let the flow advance.
        if (bruteForceEnabled) {
            bruteForce.successfulLogin(realm, user, session.getContext().getConnection());
        }
        context.success();
    }

    private void recordFailedAttempt(
            AuthenticationFlowContext context,
            UserModel user,
            BruteForceProtector bruteForce,
            boolean bruteForceEnabled) {
        if (bruteForceEnabled) {
            bruteForce.failedLogin(
                    context.getRealm(),
                    user,
                    context.getSession().getContext().getConnection());
        }
        context.getEvent().user(user).error(Errors.INVALID_USER_CREDENTIALS);
        Response challenge = buildChallenge(context, ERROR_MESSAGE_KEY);
        context.failureChallenge(AuthenticationFlowError.INVALID_CREDENTIALS, challenge);
    }

    private Response buildChallenge(AuthenticationFlowContext context, String errorKey) {
        LoginFormsProvider forms = context.form();
        if (errorKey != null) {
            forms.setError(errorKey);
        }
        return forms.createForm(PIN_TEMPLATE);
    }

    /**
     * Constant-time string compare — same length-and-content check the standard
     * Keycloak password validator uses internally. Avoids leaking PIN-prefix
     * matches via response time.
     */
    private static boolean constantTimeEquals(String a, String b) {
        if (a == null || b == null) {
            return false;
        }
        if (a.length() != b.length()) {
            return false;
        }
        int diff = 0;
        for (int i = 0; i < a.length(); i++) {
            diff |= a.charAt(i) ^ b.charAt(i);
        }
        return diff == 0;
    }

    @Override
    public boolean requiresUser() {
        // We rely on a preceding Username Form step to set context.getUser().
        return true;
    }

    @Override
    public boolean configuredFor(KeycloakSession session, RealmModel realm, UserModel user) {
        // Always return true. Returning false here would make Keycloak skip the step
        // for users without a mobile_pin attribute — which would either let them
        // through unauthenticated (bad) or surface a credential-setup page (leaks
        // which usernames have mobile access). The action() path handles missing
        // attributes by failing with a generic error.
        return true;
    }

    @Override
    public void setRequiredActions(KeycloakSession session, RealmModel realm, UserModel user) {
        // No required user actions — admin provisions PIN out-of-band.
    }

    @Override
    public void close() {
        // No resources to release.
    }
}
