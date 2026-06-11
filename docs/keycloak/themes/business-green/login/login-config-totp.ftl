<#import "template.ftl" as layout>
<#import "password-commons.ftl" as passwordCommons>
<@layout.registrationLayout displayInfo=true; section>
  <#if section = "header">
    <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
  <#elseif section = "form">
    <div class="kc-totp-wrap">
      <div class="kc-totp-card">
        <div>
          <div class="kc-totp-title">Mobile Authenticator Setup</div>
          <div class="kc-totp-sub">Scan the QR and enter the one-time code</div>
          <ul class="kc-totp-steps">
            <li>Install Google Authenticator, Microsoft Authenticator or FreeOTP</li>
            <li>Open the app and scan the barcode</li>
          </ul>
          <div class="kc-totp-qr" style="margin-top:16px;">
            <#-- QR код уже содержит data:image/png;base64, префикс -->
            <#if totp.totpSecretQrCode?has_content>
              <img alt="OTP QR Code" src="data:image/png;base64, ${totp.totpSecretQrCode}" />
            </#if>

            <#-- Ручной ввод секрета -->
            <#if totp.totpSecretEncoded?has_content>
              <div style="font-size:14px;color:#6b7280;margin-top:16px;">Cannot scan? Manual code:</div>
              <div style="font-weight:600; letter-spacing:1px;word-break:break-all;">${totp.totpSecretEncoded}</div>
            <#elseif totp.totpSecret?has_content>
              <div style="font-size:14px;color:#6b7280;margin-top:16px;">Cannot scan? Manual code:</div>
              <div style="font-weight:600; letter-spacing:1px;word-break:break-all;">${totp.totpSecret}</div>
            </#if>

            <#-- Ссылка для переключения режимов -->
            <#if mode?? && mode = "manual">
              <p style="margin-top:10px;">
                <a href="${totp.qrUrl}" id="mode-barcode">${msg("loginTotpScanBarcode")}</a>
              </p>
            <#else>
              <p style="margin-top:10px;">
                <a href="${totp.manualUrl}" id="mode-manual">${msg("loginTotpUnableToScan")}</a>
              </p>
            </#if>
          </div>
        </div>

        <div class="kc-totp-form">
          <form id="kc-totp-settings-form" action="${url.loginAction}" method="post">
            <#-- Скрытые поля необходимые для Keycloak -->
            <input type="hidden" id="totpSecret" name="totpSecret" value="${totp.totpSecret}" />
            <#if mode??><input type="hidden" id="mode" name="mode" value="${mode}"/></#if>

            <div class="input-group">
              <label for="totp">One-time code <span style="color:red;">*</span></label>
              <input id="totp" name="totp" class="input--style-3" type="text"
                     inputmode="numeric" autocomplete="off"
                     placeholder="000000" autofocus required>
              <#if messagesPerField.existsError('totp')>
                <span style="color:red;font-size:14px;margin-top:5px;display:block;">
                  ${kcSanitize(messagesPerField.get('totp'))?no_esc}
                </span>
              </#if>
            </div>

            <div class="input-group">
              <label for="userLabel">
                Device Name
                <#if totp.otpCredentials?size gte 1><span style="color:red;">*</span></#if>
              </label>
              <input id="userLabel" name="userLabel" class="input--style-3"
                     type="text" autocomplete="off"
                     placeholder="e.g. iPhone / Pixel 8">
              <#if messagesPerField.existsError('userLabel')>
                <span style="color:red;font-size:14px;margin-top:5px;display:block;">
                  ${kcSanitize(messagesPerField.get('userLabel'))?no_esc}
                </span>
              </#if>
            </div>

            <#-- Импортируем logout sessions из password-commons -->
            <div class="input-group">
              <@passwordCommons.logoutOtherSessions/>
            </div>

            <#if isAppInitiatedAction??>
              <button class="submit-btn" id="saveTOTPBtn" type="submit">
                ${msg("doSubmit")}
              </button>
              <button type="submit"
                      style="margin-top:10px;background:#6b7280;"
                      class="submit-btn"
                      id="cancelTOTPBtn" name="cancel-aia" value="true">
                ${msg("doCancel")}
              </button>
            <#else>
              <button class="submit-btn" id="saveTOTPBtn" type="submit">
                ${msg("doSubmit")}
              </button>
            </#if>

            <div style="margin-top:18px;text-align:center;">
              <a href="${url.loginRestartFlowUrl}">Back to sign in</a>
            </div>
          </form>
        </div>
      </div>
    </div>
  </#if>
</@layout.registrationLayout>
