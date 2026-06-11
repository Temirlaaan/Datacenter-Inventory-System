<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 30px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">Выход из системы</h2>
                <p style="color: #6b7280; margin-top: 10px;">
                    Вы уверены, что хотите выйти?
                </p>
            </div>

            <form action="${url.logoutConfirmAction}" method="POST">
                <input type="hidden" name="session_code" value="${logoutConfirm.code}">
                <button class="submit-btn" name="confirmLogout" value="true" type="submit">
                    Sign out
                </button>
            </form>

            <#if !logoutConfirm.skipLink && (client.baseUrl)?has_content>
            <div style="margin-top: 20px; text-align: center;">
                <a href="${client.baseUrl}" style="color: #6b7280; font-size: 14px;">
                    Отмена — вернуться к приложению
                </a>
            </div>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
