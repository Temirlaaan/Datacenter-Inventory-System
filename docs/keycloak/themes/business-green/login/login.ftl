<#import "template.ftl" as layout>
<@layout.registrationLayout; section>
<#if section = "title">
${msg("loginTitle",(realm.displayName!''))}
<#elseif section = "header">
<link rel="stylesheet" type="text/css" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css" />
<script>
function togglePassword() {
    var passwordField = document.getElementById("password");
    var eyeIcon = document.getElementById("eyeIcon");

    if (passwordField.type === "password") {
        passwordField.type = "text";
        eyeIcon.className = "fa fa-eye-slash";
    } else {
        passwordField.type = "password";
        eyeIcon.className = "fa fa-eye";
    }
}
</script>
<#elseif section = "form">
<div class="card card-3">
    <!-- Левая часть с изображением горы -->
    <div class="card-heading" style="background: url('${url.resourcesPath}/img/mountain.jpg') center center/cover no-repeat;"></div>

    <#if realm.password>
    <!-- Правая часть с формой -->
    <div class="card-body">
        <!-- Логотип TTC. Размер задаётся в business-green.css (.logo img). -->
        <div class="logo" style="text-align:center; margin-bottom: 30px;">
            <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
        </div>

        <!-- Заголовок -->
        <h2 class="title" style="text-align:center; font-size: 28px; color: #333; margin-bottom: 30px;">Добро пожаловать</h2>

        <!-- Форма входа -->
        <form id="kc-form-login" class="form form1" onsubmit="login.disabled = true; return true;" action="${url.loginAction}" method="post">

            <!-- Сообщения об ошибках -->
            <#if message?has_content && message.type != 'success'>
                <div class="alert alert-${message.type}" style="margin-bottom: 20px; padding: 10px; border-radius: 5px;
                     background: ${(message.type == 'error')?then('#fef2f2', '#fff3cd')};
                     color: ${(message.type == 'error')?then('#991b1b', '#856404')};">
                    <span>${kcSanitize(message.summary)?no_esc}</span>
                </div>
            </#if>

            <!-- Поле Username/Email -->
            <div class="input-group">
                <label for="username" style="color: #555; font-weight: 500;">
                    <#if !realm.loginWithEmailAllowed>
                        ${msg("username")}
                    <#elseif !realm.registrationEmailAsUsername>
                        ${msg("usernameOrEmail")}
                    <#else>
                        ${msg("email")}
                    </#if>
                </label>
                <div>
                    <input id="username"
                           class="input--style-3 input-width"
                           type="text"
                           placeholder="Введите имя пользователя"
                           name="username"
                           value="${(login.username!'')}"
                           autofocus
                           autocomplete="username"
                           required>
                </div>
            </div>

            <!-- Поле Password -->
            <div class="input-group" style="position: relative;">
                <label for="password" style="color: #555; font-weight: 500;">${msg("password")}</label>
                <div style="position: relative;">
                    <input class="input--style-3 input-width"
                           id="password"
                           type="password"
                           placeholder="Введите пароль"
                           name="password"
                           autocomplete="current-password"
                           required>
                    <button type="button"
                            class="toggle"
                            onclick="togglePassword()"
                            style="position: absolute; right: 10px; top: 50%; transform: translateY(-50%); background: none; border: none; cursor: pointer;">
                        <i id="eyeIcon" class="fa fa-eye" style="color: #999;"></i>
                    </button>
                </div>
            </div>

            <!-- Remember Me и Forgot Password -->
            <div style="display: flex; justify-content: space-between; align-items: center; margin: 20px 0;">
                <#if realm.rememberMe && !usernameEditDisabled??>
                <div class="checkbox">
                    <label style="display: flex; align-items: center; color: #666; font-size: 14px;">
                        <input id="rememberMe"
                               name="rememberMe"
                               type="checkbox"
                               style="margin-right: 8px;"
                               <#if login.rememberMe??>checked</#if>>
                        ${msg("rememberMe")}
                    </label>
                </div>
                </#if>

                <#if realm.resetPasswordAllowed>
                <div>
                    <a style="color:#667eea; font-size: 14px; text-decoration: none;"
                       href="${url.loginResetCredentialsUrl}">
                       ${msg("doForgotPassword")}
                    </a>
                </div>
                </#if>
            </div>

            <!-- Кнопка входа -->
            <div class="p-t-20 p-b-20" style="text-align:center;">
                <button class="submit-btn" type="submit">
                    ${msg("doLogIn")}
                </button>
            </div>
        </form>
    </div>
    </#if>
</div>
</#if>
</@layout.registrationLayout>
