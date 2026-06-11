<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 24px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div class="expired-icon" style="text-align: center; margin-bottom: 20px;">
                <span style="display: inline-flex; width: 64px; height: 64px;
                             border-radius: 50%; background: #fff7ed;
                             align-items: center; justify-content: center;
                             color: #f59e0b; font-size: 36px; line-height: 1;">⏱</span>
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px;">Сессия истекла</h2>
                <p style="color: #6b7280; margin-top: 12px;">
                    Вы слишком долго не входили в систему. Начните вход заново.
                </p>
            </div>

            <a href="${url.loginRestartFlowUrl}" class="submit-btn"
               style="display: block; text-align: center; text-decoration: none; margin-bottom: 12px;">
                Начать заново
            </a>

            <#if url.loginAction?has_content>
            <div style="text-align: center;">
                <a href="${url.loginAction}" style="color: #6b7280; font-size: 14px;">
                    Продолжить с того же места
                </a>
            </div>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
