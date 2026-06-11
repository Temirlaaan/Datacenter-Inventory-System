package com.ttc.dcinv.keycloak;

import java.util.Collections;
import java.util.List;

import org.keycloak.Config;
import org.keycloak.authentication.Authenticator;
import org.keycloak.authentication.AuthenticatorFactory;
import org.keycloak.models.AuthenticationExecutionModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.provider.ProviderConfigProperty;

/**
 * SPI factory for {@link MobilePinAuthenticator}.
 *
 * <p>Registered via {@code META-INF/services/org.keycloak.authentication.AuthenticatorFactory}.
 * Keycloak discovers this on startup and the authenticator becomes selectable
 * inside any Browser flow as "DC Inventory Mobile PIN".
 *
 * <p>Singleton: the authenticator holds no per-request state, so one instance
 * shared across the JVM is safe.
 */
public class MobilePinAuthenticatorFactory implements AuthenticatorFactory {

    /** Stable id — referenced by Keycloak flow JSON exports and Admin REST URLs.
     *  Do NOT rename in place: rebinding existing flows will fail silently. */
    public static final String PROVIDER_ID = "dcinv-mobile-pin-authenticator";

    private static final MobilePinAuthenticator SINGLETON = new MobilePinAuthenticator();

    /** Allowed requirement levels inside a flow. We deliberately omit ALTERNATIVE
     *  and CONDITIONAL — PIN check must run unconditionally when wired in. */
    private static final AuthenticationExecutionModel.Requirement[] REQUIREMENT_CHOICES = {
            AuthenticationExecutionModel.Requirement.REQUIRED,
            AuthenticationExecutionModel.Requirement.DISABLED
    };

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public Authenticator create(KeycloakSession session) {
        return SINGLETON;
    }

    @Override
    public void init(Config.Scope config) {
        // No SPI config keys yet — PIN attribute name and policies are constants.
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // Nothing to do after the realm registry is up.
    }

    @Override
    public void close() {
        // Singleton holds no resources.
    }

    @Override
    public String getDisplayType() {
        return "DC Inventory Mobile PIN";
    }

    @Override
    public String getReferenceCategory() {
        // Groups under "password" so Keycloak's BruteForceProtector treats failed
        // PIN attempts the same way it treats failed password attempts.
        return "password";
    }

    @Override
    public boolean isConfigurable() {
        return false;
    }

    @Override
    public AuthenticationExecutionModel.Requirement[] getRequirementChoices() {
        return REQUIREMENT_CHOICES;
    }

    @Override
    public boolean isUserSetupAllowed() {
        // No "user setup" page — PIN is provisioned out-of-band by an admin.
        return false;
    }

    @Override
    public String getHelpText() {
        return "Validates a 4-digit PIN from the user's 'mobile_pin' attribute. "
                + "Place after the standard Username Form step in a mobile-only flow. "
                + "Leaves the regular (AD-federated) password untouched.";
    }

    @Override
    public List<ProviderConfigProperty> getConfigProperties() {
        return Collections.emptyList();
    }
}
