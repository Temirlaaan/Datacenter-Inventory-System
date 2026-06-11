<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=true; section>
<#if section = "header">
    <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
<#elseif section = "form">
    <div class="card card-reset">
        <div class="logo" style="text-align: center; margin-bottom: 30px;">
            <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
        </div>

        <h2 class="title" style="text-align: center; font-size: 24px;">Смена пароля</h2>

        <form id="kc-passwd-update-form" action="${url.loginAction}" method="post">
            <div class="input-group">
                <label for="password-new">Новый пароль</label>
                <input type="password" id="password-new" name="password-new"
                       class="input--style-3" autofocus autocomplete="new-password"/>
            </div>

            <div class="input-group">
                <label for="password-confirm">Подтвердите пароль</label>
                <input type="password" id="password-confirm" name="password-confirm"
                       class="input--style-3" autocomplete="new-password"/>
            </div>

            <div style="margin-top: 30px;">
                <button class="submit-btn" type="submit">
                    Изменить пароль
                </button>
            </div>
        </form>
    </div>
</#if>
</@layout.registrationLayout>
