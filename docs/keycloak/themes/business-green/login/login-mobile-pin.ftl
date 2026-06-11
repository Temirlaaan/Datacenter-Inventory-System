<#--
  Mobile PIN entry page — rendered by MobilePinAuthenticator (Java SPI in
  keycloak-providers/dcinv-mobile-pin/). Sits AFTER the standard Username Form
  step, so we always have an attempted username in context.

  Form posts to ${url.loginAction} with one field: `pin` (4 digits).
  The Java side validates against the user's `mobile_pin` attribute and routes
  failures through BruteForceProtector.
-->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 24px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">Введите PIN</h2>
                <#if auth?? && auth.attemptedUsername??>
                <p style="color: #6b7280; margin-top: 8px; font-size: 14px;">
                    Пользователь: <span style="color: #1f2937; font-weight: 500;">${auth.attemptedUsername}</span>
                </p>
                </#if>
            </div>

            <#if message?has_content && message.type == 'error'>
            <div class="alert alert-error" style="margin-bottom: 20px;">
                <span>${kcSanitize(message.summary)?no_esc}</span>
            </div>
            </#if>

            <form id="kc-mobile-pin-form" action="${url.loginAction}" method="post"
                  onsubmit="document.getElementById('submitBtn').disabled = true; return true;">
                <div class="input-group">
                    <label for="pin">PIN-код</label>
                    <input id="pin"
                           name="pin"
                           type="password"
                           class="input--style-3"
                           inputmode="numeric"
                           pattern="[0-9]{4}"
                           maxlength="4"
                           minlength="4"
                           autocomplete="off"
                           autofocus
                           required
                           placeholder="● ● ● ●"
                           style="text-align: center; font-size: 28px; letter-spacing: 16px; padding: 16px;">
                </div>

                <div style="margin-top: 30px;">
                    <button id="submitBtn" class="submit-btn" type="submit">
                        Войти
                    </button>
                </div>
            </form>

            <div style="margin-top: 24px; text-align: center;">
                <a href="${url.loginRestartFlowUrl}" style="color: #6b7280; font-size: 14px;">
                    Войти под другим пользователем
                </a>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
