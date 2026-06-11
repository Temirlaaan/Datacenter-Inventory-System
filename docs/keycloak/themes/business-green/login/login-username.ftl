<#--
  Username entry page — used as the first step in the mobile flow before
  the OTP / PIN step. Standard Keycloak fields, our visual styling.

  Was missing in the original business-green theme → Keycloak fell back
  to the parent theme, which is the visual mess we're replacing here.
-->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=true; section>
<#if section = "title">
    Вход
<#elseif section = "header">
    <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
<#elseif section = "form">
    <div class="card card-reset">
        <div class="logo" style="text-align: center; margin-bottom: 30px;">
            <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
        </div>

        <div style="text-align: center; margin-bottom: 30px;">
            <h2 class="title" style="font-size: 24px;">Вход для операторов</h2>
            <p style="color: #6b7280; margin-top: 8px; font-size: 14px;">
                Введите ваш логин, чтобы продолжить
            </p>
        </div>

        <#if message?has_content && message.type == 'error'>
        <div class="alert alert-error" style="margin-bottom: 20px;">
            <span>${kcSanitize(message.summary)?no_esc}</span>
        </div>
        </#if>

        <form id="kc-form-login" action="${url.loginAction}" method="post"
              onsubmit="document.getElementById('submitBtn').disabled = true; return true;">
            <div class="input-group">
                <label for="username">
                    <#if !realm.loginWithEmailAllowed>
                        Логин
                    <#elseif !realm.registrationEmailAsUsername>
                        Логин или email
                    <#else>
                        Email
                    </#if>
                </label>
                <input id="username"
                       name="username"
                       type="text"
                       class="input--style-3"
                       autofocus
                       autocomplete="username"
                       placeholder="Например: alice"
                       value="${(login.username!'')}"
                       required>
            </div>

            <div style="margin-top: 30px;">
                <button id="submitBtn" class="submit-btn" type="submit">
                    Продолжить
                </button>
            </div>
        </form>
    </div>
</#if>
</@layout.registrationLayout>
