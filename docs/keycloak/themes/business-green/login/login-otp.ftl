<#import "template.ftl" as layout>
<@layout.registrationLayout; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 30px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">Двухфакторная аутентификация</h2>
                <p style="color: #6b7280; margin-top: 10px;">
                    Введите код из вашего приложения
                </p>
            </div>

            <form id="kc-otp-login-form" action="${url.loginAction}" method="post">
                <div class="input-group">
                    <label for="otp">Код подтверждения</label>
                    <input id="otp" name="otp" type="text" class="input--style-3"
                           autofocus autocomplete="off"
                           placeholder="000000"
                           style="text-align: center; font-size: 20px; letter-spacing: 5px;"/>
                </div>

                <div style="margin-top: 30px;">
                    <button class="submit-btn" type="submit">
                        Подтвердить
                    </button>
                </div>
            </form>

            <div style="margin-top: 30px; text-align: center;">
                <a href="${url.loginRestartFlowUrl}">Вернуться к входу</a>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
