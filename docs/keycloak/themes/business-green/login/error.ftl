<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <link href="${url.resourcesPath}/img/favicon.png" rel="icon"/>
    <#elseif section = "form">
        <div class="card card-reset">
            <div class="logo" style="text-align: center; margin-bottom: 24px;">
                <img src="${url.resourcesPath}/img/ttc.logo2.svg" alt="TTC Logo">
            </div>

            <div class="error-icon" style="text-align: center; margin-bottom: 20px;">
                <span style="display: inline-flex; width: 64px; height: 64px;
                             border-radius: 50%; background: #fef2f2;
                             align-items: center; justify-content: center;
                             color: #dc2626; font-size: 36px; font-weight: 700;
                             line-height: 1;">!</span>
            </div>

            <div style="text-align: center; margin-bottom: 30px;">
                <h2 class="title" style="font-size: 24px; color: #991b1b;">
                    Произошла ошибка
                </h2>
                <#if message?has_content>
                <p style="color: #6b7280; margin-top: 12px;">
                    ${kcSanitize(message.summary)?no_esc}
                </p>
                </#if>
            </div>

            <#if !skipLink?? && client?? && client.baseUrl?has_content>
            <a href="${client.baseUrl}" class="submit-btn"
               style="display: block; text-align: center; text-decoration: none;">
                Вернуться ко входу
            </a>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
