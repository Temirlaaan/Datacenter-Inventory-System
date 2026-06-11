<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=true; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <!-- Логотип -->
            <div class="logo" style="text-align: center; margin-bottom: 30px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <!-- Заголовок -->
            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">Забыли пароль?</h2>
                <p style="color: #6b7280; margin-top: 10px;">
                    Введите ваш email адрес, и мы отправим вам инструкции по восстановлению
                </p>
            </div>

            <!-- Форма -->
            <form id="kc-reset-password-form" action="${url.loginAction}" method="post">
                <div class="input-group">
                    <label for="username">Email адрес</label>
                    <input type="text"
                           id="username"
                           name="username"
                           class="input--style-3"
                           placeholder="example@email.com"
                           autofocus
                           required/>
                </div>

                <div style="margin-top: 30px;">
                    <button class="submit-btn" type="submit">
                        ${msg("doSubmit")}
                    </button>
                </div>
            </form>

            <!-- Ссылка назад -->
            <div style="margin-top: 30px; text-align: center;">
                <span style="color: #6b7280;">Вспомнили пароль?</span>
                <a href="${url.loginUrl}" style="margin-left: 5px;">
                    Вернуться к входу
                </a>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
