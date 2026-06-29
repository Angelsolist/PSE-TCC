import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import streamlit as st
import plotly.express as px
import unicodedata
import re
import os

@st.cache_data
def executar_query(query, params=()):
    conexao = sqlite3.connect("dados_pse.db")
    df = pd.read_sql_query(query, conexao, params=params)
    conexao.close()
    return df

# Função legada de normalização trazida para o ETL
def normalizar_texto(valor):
    texto = str(valor).strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return " ".join(texto.split())

def ingerir_indicadores_globais(arquivo, conexao):
    # Restaura a lógica original: procura por "Gráficos" ou usa a primeira
    nome_aba = "Gráficos" if "Gráficos" in arquivo.sheet_names else arquivo.sheet_names[0]
    print(f"Tentando ler indicadores globais da aba: {nome_aba}")
    
    df_geral = pd.read_excel(arquivo, sheet_name=nome_aba, header=None)
    
    score_texto = {}
    for col in df_geral.columns:
        serie = df_geral[col].dropna().astype(str)
        score_texto[col] = serie.str.contains(r"[A-Za-zÀ-ÿ]", regex=True).sum()
        
    if not score_texto:
        raise ValueError(f"A aba {nome_aba} parece estar vazia ou ilegível.")
        
    col_texto = max(score_texto, key=score_texto.get)

    melhor_coluna_valor = None
    melhor_score = -1
    for col in df_geral.columns:
        if col == col_texto: continue
        numerico = pd.to_numeric(df_geral[col], errors="coerce")
        score = numerico.notna().sum()
        if score > melhor_score:
            melhor_score = score
            melhor_coluna_valor = col
            
    if melhor_coluna_valor is None:
        raise ValueError(f"Não foi possível detectar a coluna numérica na aba {nome_aba}.")

    base = pd.DataFrame({
        "descricao": df_geral[col_texto].astype(str),
        "valor": pd.to_numeric(df_geral[melhor_coluna_valor], errors="coerce"),
    }).dropna(subset=["valor"])
    
    base["descricao_norm"] = base["descricao"].map(normalizar_texto)
    
    def buscar(termos):
        for _, row in base.iterrows():
            if all(termo in row["descricao_norm"] for termo in termos):
                return float(row["valor"])
        return None

    pactuadas = buscar(["escolas", "pactuadas"])
    atingidas = buscar(["atingidas", "pse"])
    prioritarias = buscar(["priorit", "ms"]) or buscar(["priorit"])
    
    if pactuadas is None or atingidas is None or prioritarias is None:
        raise ValueError(f"Os textos 'pactuadas', 'atingidas pse' ou 'prioritárias' não foram achados na aba {nome_aba}.")
    
    df_indicadores = pd.DataFrame([{
        'Pactuadas': pactuadas,
        'Atingidas': atingidas,
        'Prioritarias': prioritarias
    }])
    
    # Se falhar aqui, o Python vai gritar, não vai esconder o erro
    df_indicadores.to_sql("indicadores_globais", conexao, if_exists="replace", index=False)
    print("✓ Indicadores globais ingeridos com sucesso.")
    
    return nome_aba # Retorna o nome da aba para sabermos qual ignorar no próximo loop


def limpar_e_derreter_aba(df, nome_aba):
    df_view = df.copy()

    # 1. Busca dinâmica pelas âncoras (Meses e Cabeçalhos)
    idx_mes = None
    for idx, row in df_view.head(15).iterrows():
        if row.astype(str).str.contains('Data mes', case=False, na=False).any():
            idx_mes = idx
            break

    idx_cab = None
    for idx, row in df_view.head(15).iterrows():
        # Busca estrita: só para na linha se achar exatamente a estrutura de chaves
        if row.astype(str).str.contains(r'us gerencia|us gerência|inep|qtde ativ', case=False, na=False, regex=True).any():
            idx_cab = idx
            break

    if idx_cab is None:
        idx_cab = 0

    if idx_mes is not None:
        linha_meses = df_view.iloc[idx_mes].fillna("").astype(str)
    else:
        linha_meses = [""] * len(df_view.columns)

    linha_cabecalhos = df_view.iloc[idx_cab].fillna("").astype(str)

    # 2. Funde meses com métricas
    novas_colunas = []
    for cabecalho, mes in zip(linha_cabecalhos, linha_meses):
        cab_limpo = cabecalho.strip()
        mes_limpo = mes.strip()

        if mes_limpo.isdigit():
            novas_colunas.append(f"{cab_limpo} (Mês {mes_limpo})")
        else:
            novas_colunas.append(cab_limpo)

    # 3. Desambiguação de chaves
    colunas_vistas = {}
    colunas_unicas = []
    for col in novas_colunas:
        if not col or col == "nan":
            col = "Sem_Nome"
            
        if col in colunas_vistas:
            colunas_vistas[col] += 1
            colunas_unicas.append(f"{col}_{colunas_vistas[col]}")
        else:
            colunas_vistas[col] = 0
            colunas_unicas.append(col)

    df_limpo = df_view.iloc[idx_cab + 1:].reset_index(drop=True)
    df_limpo.columns = colunas_unicas
    
    colunas_validas = [col for col in df_limpo.columns if not col.startswith("Sem_Nome")]
    df_limpo = df_limpo[colunas_validas]
    df_limpo = df_limpo.dropna(axis=1, how='all')

    # 4. Padronização de Esquema e Tratamento de Células Mescladas ANTES do Melt
    # 4. Padronização de Esquema e Tratamento de Células Mescladas ANTES do Melt
    renames = {}
    vistos = set()
    for col in df_limpo.columns:
        # Puxa para minúsculo e arranca espaços em branco das bordas
        col_lower = str(col).lower().strip()
        
        if ('gerencia' in col_lower or 'gerência' in col_lower) and 'Us_gerencia' not in vistos:
            renames[col] = 'Us_gerencia'
            vistos.add('Us_gerencia')
        elif col_lower in ['us', 'u.s', 'unidade de saude', 'unidade de saúde'] and 'Us' not in vistos:
            renames[col] = 'Us'
            vistos.add('Us')
        elif 'inep' in col_lower and 'Inep' not in vistos:
            renames[col] = 'Inep'
            vistos.add('Inep')
    
    df_limpo = df_limpo.rename(columns=renames)

    # Injeta colunas faltantes se a aba for defectiva
    for col_obrigatoria in ['Us_gerencia', 'Us', 'Inep']:
        if col_obrigatoria not in df_limpo.columns:
            df_limpo[col_obrigatoria] = None

    # Definição estrita das chaves estruturais
    colunas_chave = ['Us_gerencia', 'Us', 'Inep']
    
    # As colunas de valor serão todas as que não forem chaves nem marcadores booleanos
    colunas_valor = [
        col for col in df_limpo.columns 
        if col not in colunas_chave
        and "sim" not in col.lower()
        and "não" not in col.lower()
    ]

    # --- O PONTO CRÍTICO DE CORREÇÃO (Forward Fill e Normalização Estrita) ---
    import numpy as np
    
    df_limpo['Us_gerencia'] = df_limpo['Us_gerencia'].replace(r'^\s*$', np.nan, regex=True).ffill()
    # Converte tudo para maiúsculo e arranca espaços fantasmas. Nulos viram tag visível.
    df_limpo['Us_gerencia'] = df_limpo['Us_gerencia'].fillna('NAO_INFORMADA').astype(str).str.strip().str.upper()

    df_limpo['Us'] = df_limpo['Us'].replace(r'^\s*$', np.nan, regex=True).ffill()
    df_limpo['Us'] = df_limpo['Us'].fillna('NAO_INFORMADA').astype(str).str.strip().str.upper()

    # Prevenção: Se a aba não tiver colunas de dados válidas, pula a ingestão
    if not colunas_valor:
        return pd.DataFrame()

    # 5. Transformação Relacional (Melt)
    df_relacional = df_limpo.melt(
        id_vars=colunas_chave,
        value_vars=colunas_valor,
        var_name="Metrica_Bruta",
        value_name="Valor"
    )

    # Extrai o mês e limpa a métrica
    df_relacional['Mês'] = df_relacional['Metrica_Bruta'].apply(
        lambda x: int(re.search(r'Mês (\d+)', x).group(1)) if re.search(r'Mês (\d+)', x) else None
    )
    df_relacional['Metrica'] = df_relacional['Metrica_Bruta'].str.replace(r' \(Mês \d+\)', '', regex=True)
    
    df_relacional = df_relacional.drop(columns=['Metrica_Bruta'])
    
    # Tratamento rigoroso da coluna de valores
    df_relacional['Valor'] = df_relacional['Valor'].astype(str).str.replace(',', '.', regex=False)
    df_relacional['Valor'] = pd.to_numeric(df_relacional['Valor'].replace(['-', ''], pd.NA), errors='coerce')
    df_relacional['Valor'] = df_relacional['Valor'].fillna(0)
    df_relacional['Acao_PSE'] = renomeia_abas_para_exibicao(nome_aba)

    return df_relacional[['Us_gerencia', 'Us', 'Inep', 'Valor', 'Mês', 'Metrica', 'Acao_PSE']]

def renomeia_abas_para_exibicao(aba):
    # Dicionário garante a relação exata e performática de chave -> valor
    mapeamento = {
        "antro": "Antropometria",
        "alim saudavel": "Alimentação saudável",
        "pracorporais": "Práticas corporais (atividades físicas)",
        "smental": "Saúde mental",
        "violências": "Prevenção de violências",
        "sbucal": "Saúde bucal",
        "sauditiva": "Saúde auditiva",
        "socular": "Saúde ocular",
        "eduamb": "Educação ambiental",
        "dengue": "Combate à dengue",
        "culturapaz": "Cultura de paz e direitos humanos",
        "ssexual": "Saúde sexual e reprodutiva",
        "vacina": "Situação vacinal",
        "negligenciados": "Agravos negligenciados",
        "álcool e drogas": "Dependência química (álcool e outras drogas)",
        "sem saude na escola": "Semana Saúde na Escola"
    }
    
    aba_lower = aba.lower()
    for chave, valor_renomeado in mapeamento.items():
        if chave in aba_lower:
            return valor_renomeado
            
    # Retorno de segurança (fallback). Se não achar correspondência, 
    # retorna o nome original da aba em vez de propagar 'None' para o banco.
    return aba

def processar_upload_excel(arquivo_upload):
    """Processa o arquivo em memória vindo do Streamlit e gera o SQLite."""
    conexao = sqlite3.connect("dados_pse.db")
    
    # O Pandas ExcelFile aceita o buffer do Streamlit diretamente
    arquivo = pd.ExcelFile(arquivo_upload)
    
    # 1. Ingestão Macro
    aba_macro = ingerir_indicadores_globais(arquivo, conexao)
    
    # 2. Ingestão Micro
    abas_micro = [aba for aba in arquivo.sheet_names if aba != aba_macro] 
    tabelas_finais = []
    
    for aba in abas_micro:
        df_bruto = pd.read_excel(arquivo, sheet_name=aba, header=None)
        df_processado = limpar_e_derreter_aba(df_bruto, aba)
        if not df_processado.empty:
            tabelas_finais.append(df_processado)
        
    if tabelas_finais:
        df_final = pd.concat(tabelas_finais, ignore_index=True)
        df_final.to_sql("registros_pse", conexao, if_exists="replace", index=False)
        sucesso = True
        linhas = len(df_final)
    else:
        sucesso = False
        linhas = 0
        
    conexao.close()
    return sucesso, linhas    

def carregar_dicionario_escolas():
    """Lê o CSV e prepara um dicionário Nome da Escola -> Código Inep"""
    try:
        # Substitua pelas colunas EXATAS do seu arquivo CSV
        COLUNA_NOME_CSV = "Escola" 
        COLUNA_INEP_CSV = "Código INEP"
        
        df_csv = pd.read_csv("tabelas_escolas.csv", dtype=str)
        
        # Higienização: Letras maiúsculas e sem espaços nas bordas para garantir o "match"
        df_csv['Nome_CSV_Clean'] = df_csv[COLUNA_NOME_CSV].astype(str).str.strip().str.upper()
        df_csv['Código INEP'] = df_csv[COLUNA_INEP_CSV].astype(str).str.strip().str.replace('.0', '', regex=False)
        
        # Retorna apenas o que importa
        return df_csv[['Nome_CSV_Clean', 'Código INEP']]
    except Exception as e:
        st.error(f"Erro ao carregar o CSV de escolas: {e}")
        return pd.DataFrame(columns=['Nome_CSV_Clean', 'Código INEP'])

def cria_novos_indicadores(df_dados, pactuadas, acoes_prioritarias, acoes_necessarias):

    escolas_com_prioritarias = set()

    for escola in df_dados["Nome da Escola"].unique():
        acoes_escola = set(df_dados[df_dados["Nome da Escola"] == escola]["Ação"].unique())
        if sum(1 for Ação in acoes_prioritarias if Ação in acoes_escola) >= acoes_necessarias:
            escolas_com_prioritarias.add(escola)

    indicador_novo = len(escolas_com_prioritarias)
    st.write(f"**Quantidade de escolas com ações registradas em pelo menos {acoes_necessarias} das 5 ações prioritárias:** {indicador_novo}")
    st.write(f"**Porcentagem de cobertura de {acoes_necessarias} ações: {indicador_novo/pactuadas * 100:.2f}%**")
    
    # 1. Carrega o dicionário para cruzar os INEPs nesta visão (já que o df_dados global não os possui)
    df_dicionario = carregar_dicionario_escolas()
    
    dados_faltantes = []

    for escola in escolas_com_prioritarias:
        acoes_escola = set(df_dados[df_dados["Nome da Escola"] == escola]["Ação"].unique())
        acoes_faltantes = [Ação for Ação in acoes_prioritarias if Ação not in acoes_escola]
        
        if acoes_faltantes:
            dados_faltantes.append({
                "Nome_Original": escola,
                "Nome_SQL_Clean": str(escola).strip().upper(), # Higienização para a chave de join
                "Ações Faltantes": ", ".join(acoes_faltantes)
            })
    
    df_acoes_faltantes = pd.DataFrame(dados_faltantes)

    # 2. Executa o cruzamento se houver dados
    if not df_acoes_faltantes.empty:
        df_acoes_faltantes = pd.merge(
            df_acoes_faltantes, 
            df_dicionario, 
            how='left', 
            left_on='Nome_SQL_Clean', 
            right_on='Nome_CSV_Clean'
        )
        
        # 3. Trata nulos e cria a coluna consolidada final
        df_acoes_faltantes['Código INEP'] = df_acoes_faltantes['Código INEP'].fillna('SEM CÓDIGO INEP').astype(str)
        df_acoes_faltantes['Escola'] = df_acoes_faltantes['Código INEP'] + " - " + df_acoes_faltantes['Nome_Original']
        
        # Filtra apenas o essencial para a tela
        df_acoes_faltantes = df_acoes_faltantes[["Escola", "Ações Faltantes"]]
    else:
        # Retorno de segurança para evitar erro de componente vazio no Streamlit
        df_acoes_faltantes = pd.DataFrame(columns=["Escola", "Ações Faltantes"])
    
    return df_acoes_faltantes


def apresenta_indicadores(df_dados, pactuadas, num_acoes_prioritarias):
    # Contagem de escolas diferentes presentes no dataset
    st.markdown("---")
    st.markdown("**Indicadores**", text_alignment="center", width='stretch')

    total_escolas = df_dados.groupby("Nome da Escola", dropna=False).ngroups
    #dataframe que pega a escola e a ação para buscar, por ação, qual escola não fez ação x
    mini_df = df_dados[["Nome da Escola", "Ação", "Valor"]]
    total_escolas_sem_acoes = 0

    for escola, grupo in mini_df.groupby("Nome da Escola"):
        if (grupo["Valor"] == 0).all():
            #st.write(f"Escola sem ações de **'{grupo['Ação'].iloc[0]}'** registradas: {escola}")
            total_escolas_sem_acoes += 1
    
    total_escolas_com_acoes = total_escolas - total_escolas_sem_acoes
    #st.write(f"**Total de escolas com ao menos 1 ação registrada:** {total_escolas_com_acoes}")

    #indicador 1: ao menos 1 ação em todas as escolas, dentre as 14. PORCENTAGEM
    indicador_um = total_escolas_com_acoes/pactuadas * 100
    st.write(f"**Indicador 1: Quantidade de escolas com ao menos 1 ação registrada:** {total_escolas_com_acoes}")
    st.write(f"**Porcentagem de cobertura do 1º indicador:** {indicador_um:.2f}%")

    #indicador 2: quantidade de escolas que fizeram as 5 ações prioritarias
    acoes_prioritarias = ["Saúde mental", 
                          "Situação vacinal", 
                          "Cultura de paz e direitos humanos", 
                          "Saúde sexual e reprodutiva", 
                          "Alimentação saudável"]
    
    # 2. Isola apenas os registros onde a ação DE FATO ocorreu
    df_acoes_reais = df_dados[df_dados["Valor"] > 0]

    # indicador 2: quantidade de escolas que fizeram as 5 ações prioritárias
    escolas_com_todas_prioritarias = set()

    for escola in df_acoes_reais["Nome da Escola"].unique():
        acoes_escola = set(df_acoes_reais[df_acoes_reais["Nome da Escola"] == escola]["Ação"].unique())
        if all(Ação in acoes_escola for Ação in acoes_prioritarias):
            escolas_com_todas_prioritarias.add(escola)
            #st.write(f"Escola com todas as 5 ações prioritárias: {escola}") # Descomente se quiser debugar

    indicador_dois = len(escolas_com_todas_prioritarias)
    st.write(f"**Indicador 2: Quantidade de escolas com ações registradas em todas as 5 ações prioritárias:** {indicador_dois}\n")
    st.write(f"**Porcentagem de cobertura do 2º indicador: {indicador_dois/pactuadas * 100:.2f}%**")
    #fala qual a escola
    st.write(f"**Escolas com todas as 5 ações prioritárias registradas:** {', '.join(escolas_com_todas_prioritarias) if escolas_com_todas_prioritarias else 'Nenhuma'}")
    
    st.markdown("---")
    #cria_novos_indicadores(df_dados, pactuadas, acoes_prioritarias, 5)
    df_acoes_faltantes = cria_novos_indicadores(df_dados, pactuadas, acoes_prioritarias, num_acoes_prioritarias)

    st.markdown(f"**Escolas com pelo menos {num_acoes_prioritarias} das 5 ações prioritárias registradas:**")
    st.dataframe(df_acoes_faltantes, width='stretch', hide_index=True)


    st.markdown("---")

def apresenta_escolas_sem_acoes(df_dados):

    acoes_prioritarias = ["Saúde mental", 
                          "Situação vacinal", 
                          "Cultura de paz e direitos humanos", 
                          "Saúde sexual e reprodutiva", 
                          "Alimentação saudável"]
    meses = {1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
             7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"}

    #cria novo campo no dataframe que renomeia mes em numero para mes em nome

    df_dados['Mês_Nome'] = df_dados['Mês'].map(meses)

    # novo bloco BUSCA POR ESCOLA. MOSTRA QUAIS AÇÕES FORAM REALIZADAS NA ESCOLA E O MÊS E A REGIAO E A US
    df_escolas = df_dados["Nome da Escola"].unique()
    st.markdown("### Busca por Escola", text_alignment="center")
    escola_selecionada = st.selectbox("Selecione a escola para ver detalhes:", options=df_escolas)
    if escola_selecionada:
        detalhes_escola = df_dados[df_dados["Nome da Escola"] == escola_selecionada][["Coordenadoria de Região", "Unidade de Saúde", "Mês_Nome", "Ação", "Valor"]]
        st.markdown(f"**Detalhes para a escola: {escola_selecionada}**")
        if detalhes_escola.empty:
            st.warning("Nenhum dado encontrado para esta escola.")
        else:
            detalhes_escola = detalhes_escola[detalhes_escola["Valor"] > 0]
            st.dataframe(detalhes_escola, width='stretch', hide_index=True)
    #mostra quais acoes prioritarias foram feitas, caso tenham sido
        acoes_feitas = detalhes_escola[detalhes_escola["Valor"] > 0]["Ação"].unique()
        acoes_prioritarias_feitas = [Ação for Ação in acoes_prioritarias if Ação in acoes_feitas]
        if acoes_prioritarias_feitas:
            st.markdown(f"**Ações prioritárias realizadas na escola: {', '.join(acoes_prioritarias_feitas)}**")
        else:
            st.markdown("**Nenhuma ação prioritária registrada para esta escola.**")

    st.markdown("---")


def renderizar_dashboard_geral(indicadores, df_dados, pizza_atividades_df, pizza_escolas_df, num_acoes, num_acoes_prioritarias):
    """Renderiza a visão macro (similar à imagem de referência)"""
    st.markdown("### Visão Geral", text_alignment="center")
    st.write("\n\n\n\n")
    
    # 1. Linha de KPIs no topo
    col1, col2, col3 = st.columns(3)
    pactuadas = int(indicadores["Pactuadas"].iloc[0])
    atingidas = int(indicadores["Atingidas"].iloc[0])
    prioritarias = int(indicadores["Prioritarias"].iloc[0])
    
    col1.metric("Escolas Pactuadas", pactuadas)
    col2.metric("Atingidas PSE", atingidas, f"{(atingidas / pactuadas) * 100:.2f}%", "off")
    col3.metric("Prioritárias MS", prioritarias, f"{(prioritarias / pactuadas) * 100:.2f}%", "off")

    #Gera os gráficos de região para dados gerais e soma o total de ações por regiao
    df_dados_regiao = df_dados.groupby("Coordenadoria de Região")["Valor"].sum().reset_index()
    df_dados_regiao = df_dados_regiao.sort_values("Valor", ascending=True)
    #Exclui a "regiao" 'data mes'
    df_dados_regiao = df_dados_regiao[df_dados_regiao["Coordenadoria de Região"] != "Data mes"]

    apresenta_indicadores(df_dados, pactuadas, num_acoes_prioritarias)

    apresenta_escolas_sem_acoes(df_dados)

    # 2. Grid de Gráficos (lado a lado)
    col_chart1, col_chart2 = st.columns(2)
    
    with col_chart1:
        st.markdown(f"**Volume por Ação (Top {num_acoes})**", text_alignment="center")
        # Correção: Use barras horizontais ao invés de pizza para muitas categorias
        df_top10 = pizza_atividades_df.nlargest(num_acoes, 'valor').sort_values('valor', ascending=True)
        fig_bar = px.bar(df_top10, x="valor", y="Ação", orientation='h')
        st.plotly_chart(fig_bar, width='stretch')

        st.markdown("---")

        fig_bar_regiao = px.bar(df_dados_regiao, y="Coordenadoria de Região", x="Valor", title="Total de Ações por Coordenadoria de Região", text_auto=True, orientation='h')
        fig_bar_regiao.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
        st.plotly_chart(fig_bar_regiao, width='stretch')

    with col_chart2:
        st.markdown(f"**Volume de escolas atingidas por ação (Top {num_acoes})**", text_alignment="center")
        df_top10_escolas = pizza_escolas_df.nlargest(num_acoes, 'valor').sort_values('valor', ascending=True)
        fig_bar_escolas = px.bar(df_top10_escolas, x="valor", y="Ação", orientation='h')
        st.plotly_chart(fig_bar_escolas, width='stretch')

        st.markdown("---")

        fig_pizza_regiao = px.pie(df_dados_regiao, names="Coordenadoria de Região", values="Valor", title="Distribuição percentual por Coordenadoria de Região")
        fig_pizza_regiao.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
        st.plotly_chart(fig_pizza_regiao, width='stretch')



    categorias = [
        "Escolas pactuadas",
        "Escolas atingidas pelas ações do PSE",
        "Escolas com ações prioritárias do MS",
    ]
    valores = [
        indicadores["Pactuadas"].iloc[0],
        indicadores["Atingidas"].iloc[0],
        indicadores["Prioritarias"].iloc[0],
    ]
    st.markdown("---")
    fig, ax = plt.subplots(figsize=(10, 5))
    barras = ax.bar(categorias, valores, color=["#2E86AB", "#1FAA59", "#FF9F1C"])
    ax.set_title("Ações PSE - Porto Alegre 2025", fontsize=20, pad=12, loc='center')
    ax.set_ylabel("Quantidade de escolas")
    ax.tick_params(axis="x", rotation=15)

    st.pyplot(fig)

def renderizar_dashboard_acao(acao_nome, num_acoes):
    """Renderiza a visão detalhada de uma ação específica com filtros em cascata"""
    st.markdown(f"### Detalhamento: {acao_nome}")
    
    # 1. Carrega dados do SQL
    df_dados = executar_query("""
        SELECT Us_gerencia as "Coordenadoria de Região", Us as "Unidade de Saúde", Inep as "Nome da Escola", Mês as "Mês", Valor
        FROM registros_pse
        WHERE Acao_PSE = ?
    """, (acao_nome,))

    if df_dados.empty:
        st.warning(f"Não foram encontrados dados da ação {acao_nome}")
        return

    # 2. Carrega o dicionário do CSV
    df_dicionario = carregar_dicionario_escolas()

    # 3. Higieniza a coluna de nome do SQL (chamada de 'Inep') para cruzar com segurança
    df_dados['Nome_SQL_Clean'] = df_dados['Nome da Escola'].astype(str).str.strip().str.upper()

    # 4. Cruzamento Nome -> Nome
    df_dados = pd.merge(
        df_dados, 
        df_dicionario, 
        how='left', 
        left_on='Nome_SQL_Clean', 
        right_on='Nome_CSV_Clean'
    )

    # Trata as escolas que mesmo com a higienização não bateram com o CSV e força tipo string
    df_dados['Código INEP'] = df_dados['Código INEP'].fillna('SEM CÓDIGO INEP').astype(str)
    
    # PROTEÇÃO ADICIONADA: Trata nulos vindos de células vazias na planilha original
    df_dados['Nome da Escola'] = df_dados['Nome da Escola'].fillna('SEM NOME NA PLANILHA').astype(str)

    # Cria a coluna consolidada com estabilidade garantida (string + string + string)
    df_dados['Código INEP - Nome da Escola'] = df_dados['Código INEP'] + " - " + df_dados['Nome da Escola']

    st.markdown("---")
    st.markdown("**Filtros de Análise**")
    
    col_filtro1, col_filtro2, col_filtro3 = st.columns(3)

    with col_filtro1:
        lista_gerencias = ["Todas"] + sorted(df_dados["Coordenadoria de Região"].dropna().unique().tolist())
        gerencia_selecionada = st.selectbox("Coordenadoria de Região:", lista_gerencias)

    df_f1 = df_dados if gerencia_selecionada == "Todas" else df_dados[df_dados["Coordenadoria de Região"] == gerencia_selecionada]

    with col_filtro2:
        lista_us = ["Todas"] + sorted(df_f1["Unidade de Saúde"].dropna().unique().tolist())
        us_selecionada = st.selectbox("Unidade de Saúde (US):", lista_us)

    df_f2 = df_f1 if us_selecionada == "Todas" else df_f1[df_f1["Unidade de Saúde"] == us_selecionada]

    with col_filtro3:
        lista_escolas = ["Todas"] + sorted(df_f2["Código INEP - Nome da Escola"].dropna().unique().tolist())
        escola_selecionada = st.selectbox("Escola (INEP - Nome):", lista_escolas)

    # Armazena o resultado dos filtros espaciais
    df_f3 = df_f2 if escola_selecionada == "Todas" else df_f2[df_f2["Código INEP - Nome da Escola"] == escola_selecionada]

    # --- NOVO BLOCO: Filtros Temporais (Cascata Contínua) ---
    
    # 1. Garante que os meses sejam lidos como números puros para permitir filtragem de range
    df_f3 = df_f3.copy() # Evita o alerta 'SettingWithCopyWarning' do Pandas
    df_f3['Mes_Num'] = pd.to_numeric(df_f3['Mês'], errors='coerce')

    # Identifica apenas os meses que realmente contêm dados após os filtros de localização
    meses_disponiveis = sorted(df_f3['Mes_Num'].dropna().unique().astype(int).tolist())

    if not meses_disponiveis:
        st.info("A seleção atual não possui dados mensais estruturados para filtragem de período.")
        df_final = df_f3 # Pula a filtragem temporal e segue em frente
    else:
        st.markdown("**Período de Análise**")
        col_mes1, col_mes2 = st.columns(2)
        
        # Dicionário para traduzir o número para o usuário final
        nomes_meses = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho',
                       7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
        
        with col_mes1:
            mes_inicio = st.selectbox(
                "Mês Inicial:", 
                options=meses_disponiveis, 
                format_func=lambda x: nomes_meses.get(x, str(x)) # Exibe 'Janeiro' mas passa '1' para a variável
            )
        
        with col_mes2:
            # Trava de segurança: O mês final só exibe opções maiores ou iguais ao mês inicial
            opcoes_fim = [m for m in meses_disponiveis if m >= mes_inicio]
            
            mes_fim = st.selectbox(
                "Mês Final:", 
                options=opcoes_fim, 
                index=len(opcoes_fim)-1, # Seleciona o último mês da lista por padrão
                format_func=lambda x: nomes_meses.get(x, str(x))
            )

        # 2. Aplica a filtragem matemática definitiva
        df_final = df_f3[(df_f3['Mes_Num'] >= mes_inicio) & (df_f3['Mes_Num'] <= mes_fim)]

    st.markdown("---")

    if df_final.empty:
        st.info("Nenhum registro encontrado para a combinação de filtros selecionada.")
        return

    mes_nomes = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho',
                 7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
    
    df_final['Mês_nome'] = df_final['Mes_Num'].map(mes_nomes).fillna("Não Informado")
    # 3. Renderiza a Interface com os dados rigorosamente filtrados
    col_metric, col_tabela = st.columns([1, 2])

    with col_metric:
        col_metric.subheader("Indicadores da Seleção Atual")
        col_total_escolas, col_total_acoes = st.columns(2)
        # Conta Ineps únicos baseados no dataframe já filtrado
        total_escolas_acao = df_final["Nome da Escola"].nunique()
        total_acoes_acao = df_final["Valor"].sum()
        with col_total_escolas:
            st.metric("Escolas Contempladas (Seleção Atual)", total_escolas_acao) 
        with col_total_acoes:
            st.metric("Total de Ações Registradas (Seleção Atual)", int(total_acoes_acao))

        # Agrupa os dados para a tabela lateral
        df_escolas_totais = df_final.groupby(["Coordenadoria de Região", "Unidade de Saúde", "Nome da Escola", "Código INEP"])["Valor"].sum().reset_index()
        df_escolas_totais = df_escolas_totais.rename(columns={"Valor": "Total_Acoes"})
        
        st.markdown("**Volume total na seleção atual:**")
        st.dataframe(df_escolas_totais, width='stretch', hide_index=True)

    with col_tabela:
        # Agrupa mantendo a chave numérica para garantir a ordenação cronológica rigorosa
        df_plot = df_final.groupby(["Mes_Num", "Mês_nome"])["Valor"].sum().reset_index()
        df_plot = df_plot.sort_values("Mes_Num")

        # Constrói um título inteligente para o gráfico para o usuário saber o que está olhando
        titulo_grafico = "Evolução Mensal"
        if escola_selecionada != "Todas":
            titulo_grafico += f" - INEP: {escola_selecionada}"
        elif us_selecionada != "Todas":
            titulo_grafico += f" - US: {us_selecionada}"
        elif gerencia_selecionada != "Todas":
            titulo_grafico += f" - Região: {gerencia_selecionada}"
        else:
            titulo_grafico += " (Geral)"

        fig_line = px.line(df_plot, x="Mês_nome", y="Valor", title=titulo_grafico, markers=True)
        fig_line.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
        st.plotly_chart(fig_line, width='stretch')
        
        st.info("Meses não informados no gráfico acima não aparecem pois não possuem ações registradas.")

    # Tratamento para abas que não possuem dados mensais estruturados (ex: Dengue)
    if df_dados["Mês"].isna().all():
        df_exibir = df_dados.drop(columns=["Mês"]).rename(columns={"Valor": "Total Registrado"})
    else:
        # Preenche temporariamente nulos caso haja mistura de dados mensais e globais
        df_dados["Mês"] = df_dados["Mês"].fillna("Não Informado")
        df_pivot = df_dados.pivot_table(
            index=["Coordenadoria de Região", "Unidade de Saúde", "Nome da Escola", "Código INEP"],
            columns="Mês",
            values="Valor",
            aggfunc="sum"
        ).reset_index()

        meses = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", 
                     "julho", 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']

        # Transforma os números dos meses em strings de cabeçalho limpas
        df_pivot.columns = [
            #renomeia mes 1 com janeiro mes 2 com fevereiro etc
            f"{meses[int(float(col)) - 1]}" if str(col).replace('.0', '').isdigit() and 1 <= int(float(col)) <= 12 else str(col)
            for col in df_pivot.columns
        ]
        df_exibir = df_pivot[["Coordenadoria de Região", "Unidade de Saúde", "Nome da Escola", "Código INEP"] + [col for col in df_pivot.columns if col not in ["Coordenadoria de Região", "Unidade de Saúde", "Nome da Escola", "Código INEP"]]]

    gera_graficos_regiao_us_escola(df_dados, df_final, num_acoes)

    #Mostra só o essencial da filtrada
    df_final = df_final[["Coordenadoria de Região", "Unidade de Saúde", "Código INEP - Nome da Escola", "Mês", "Valor"]]

    exibir_tabela_filtrada(df_final)
    exibir_tabela_completa(df_exibir)


def gera_graficos_regiao_us_escola(df_dados, df_final, num_acoes):
    #gráficos por região, us e escola
    
    st.markdown("---")
    st.markdown("**Ações por Região, US e Escola (após aplicação dos filtros)**", text_alignment="center")
    
    col1, col2 = st.columns(2)
    with col1:
        df_regiao = df_dados.groupby("Coordenadoria de Região")["Valor"].sum().reset_index()
        fig_bar_regiao = px.bar(df_regiao, y="Coordenadoria de Região", x="Valor", title="Total de Ações por Coordenadoria de Região", text_auto=True, orientation='h')
        fig_bar_regiao.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
        st.plotly_chart(fig_bar_regiao, width='stretch')
    with col2:
        fig_pizza_regiao = px.pie(df_regiao, names="Coordenadoria de Região", values="Valor", title="Distribuição percentual por Coordenadoria de Região")
        fig_pizza_regiao.update_layout(margin=dict(l=20, r=0, t=40, b=0), height=400)
        st.plotly_chart(fig_pizza_regiao, width='stretch')
    st.info("Os gráficos acima não são afetados pelos filtros aplicados no início da página, pois têm o objetivo de mostrar a distribuição geral por região para a ação selecionada. Se uma região não aparecer, é porque ela não possui registros para esta ação específica.")

    df_us = df_final.groupby("Unidade de Saúde")["Valor"].sum().reset_index()
    df_top_us = df_us.nlargest(num_acoes, 'Valor').sort_values('Valor', ascending=True)
    fig_bar_us = px.bar(df_top_us, y="Unidade de Saúde", x="Valor", title=f"Ações por Unidade de Saúde (Top {num_acoes})", text_auto=True, orientation='h')
    fig_bar_us.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
    st.plotly_chart(fig_bar_us, width='stretch')
    st.info("O gráfico acima mostra as US de acordo com os filtros aplicados no início da página. Se uma US não aparecer, é porque ela não possui registros para a combinação de filtros selecionada.")

    df_escola = df_final.groupby("Código INEP - Nome da Escola")["Valor"].sum().reset_index()
    df_top_escolas = df_escola.nlargest(num_acoes, 'Valor').sort_values('Valor', ascending=True)
    fig_bar_escola = px.bar(df_top_escolas, y="Código INEP - Nome da Escola", x="Valor", title=f"Ações por Escola (Top {num_acoes})", text_auto=True, orientation='h')
    fig_bar_escola.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
    st.plotly_chart(fig_bar_escola, width='stretch')
    st.info("O gráfico acima mostra as escolas de acordo com os filtros aplicados no início da página. Se uma escola não aparecer, é porque ela não possui registros para a combinação de filtros selecionada.")

def exibir_tabela_filtrada(df):
    st.write("\n\n")

    mes_nomes = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho',
                 7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
    df['Mês_nome'] = df['Mês'].map(mes_nomes).fillna("Não Informado")

    df = df[["Coordenadoria de Região", "Unidade de Saúde", "Código INEP - Nome da Escola", "Mês_nome", "Valor"]].rename(columns={"Mês_nome": "Mês", "Valor": "Total Registrado"})
    #penas valores positivos
    df = df[df["Total Registrado"] > 0]
    col1, col2, col3 = st.columns(3)
    
    with col2:
        if "mostrar_tabela_filtrada" not in st.session_state:
            st.session_state["mostrar_tabela_filtrada"] = False

        if st.button("Exibir Tabela dos registros filtrados"):
            st.session_state["mostrar_tabela_filtrada"] = not st.session_state["mostrar_tabela_filtrada"]
    
    if st.session_state["mostrar_tabela_filtrada"]:
        st.markdown("**Tabela dos registros filtrados (após aplicação dos filtros)**", text_alignment="center")
        st.dataframe(df, width='stretch', hide_index=True)

def exibir_tabela_completa(df):
    #Dá enters
    st.write("\n\n")
    #tabela_final = df.copy()
    #tabela_final = tabela_final[["Coordenadoria de Região", "Unidade de Saúde", "Código INEP - Nome da Escola", "Total Registrado", "Nome da Escola", "Código INEP"]]
    #st.markdown("**Tabela Completa dos registros desta ação**", text_alignment="center")
    col1, col2, col3 = st.columns(3)
    with col2:
        if "mostrar_tabela" not in st.session_state:
            st.session_state["mostrar_tabela"] = False

        if st.button("Exibir Tabela Completa dos registros desta ação", ):
            st.session_state["mostrar_tabela"] = not st.session_state["mostrar_tabela"]

    if st.session_state["mostrar_tabela"]:
        st.dataframe(df, width='stretch', hide_index=True)

def renderizar_dashboard_multiplas_acoes(df_dados, acoes_disponiveis, num_acoes):
    st.markdown("### Comparativo de Múltiplas Ações", text_alignment="center")
    
    acoes_selecionadas = st.multiselect(
        "Selecione as ações que deseja comparar:", 
        options=acoes_disponiveis,
        default=acoes_disponiveis[:2] if len(acoes_disponiveis) >= 2 else acoes_disponiveis
    )

    if not acoes_selecionadas:
        st.warning("Selecione pelo menos uma ação para visualizar o gráfico.")
        return

    # 1. Filtro inicial das ações selecionadas
    df_filtrado_acoes = df_dados[df_dados["Ação"].isin(acoes_selecionadas)].copy()

    # 2. Carrega dicionário e cruza chaves (Motor idêntico ao da ação específica)
    df_dicionario = carregar_dicionario_escolas()
    df_filtrado_acoes['Nome_SQL_Clean'] = df_filtrado_acoes['Nome da Escola'].astype(str).str.strip().str.upper()
    df_filtrado_acoes = pd.merge(
        df_filtrado_acoes, 
        df_dicionario, 
        how='left', 
        left_on='Nome_SQL_Clean', 
        right_on='Nome_CSV_Clean'
    )

    df_filtrado_acoes['Código INEP'] = df_filtrado_acoes['Código INEP'].fillna('SEM CÓDIGO INEP').astype(str)
    df_filtrado_acoes['Nome da Escola'] = df_filtrado_acoes['Nome da Escola'].fillna('SEM NOME NA PLANILHA').astype(str)
    df_filtrado_acoes['Código INEP - Nome da Escola'] = df_filtrado_acoes['Código INEP'] + " - " + df_filtrado_acoes['Nome da Escola']

    st.markdown("---")
    st.markdown("**Filtros de Análise**")
    
    # 3. Filtros Hierárquicos
    col_filtro1, col_filtro2, col_filtro3 = st.columns(3)

    with col_filtro1:
        lista_gerencias = ["Todas"] + sorted(df_filtrado_acoes["Coordenadoria de Região"].dropna().unique().tolist())
        gerencia_selecionada = st.selectbox("Coordenadoria de Região:", lista_gerencias)

    df_f1 = df_filtrado_acoes if gerencia_selecionada == "Todas" else df_filtrado_acoes[df_filtrado_acoes["Coordenadoria de Região"] == gerencia_selecionada]

    with col_filtro2:
        lista_us = ["Todas"] + sorted(df_f1["Unidade de Saúde"].dropna().unique().tolist())
        us_selecionada = st.selectbox("Unidade de Saúde (US):", lista_us)

    df_f2 = df_f1 if us_selecionada == "Todas" else df_f1[df_f1["Unidade de Saúde"] == us_selecionada]

    with col_filtro3:
        lista_escolas = ["Todas"] + sorted(df_f2["Código INEP - Nome da Escola"].dropna().unique().tolist())
        escola_selecionada = st.selectbox("Escola (INEP - Nome):", lista_escolas)

    df_f3 = df_f2 if escola_selecionada == "Todas" else df_f2[df_f2["Código INEP - Nome da Escola"] == escola_selecionada]

    # 4. Filtros Temporais
    df_f3 = df_f3.copy()
    df_f3['Mes_Num'] = pd.to_numeric(df_f3['Mês'], errors='coerce')
    meses_disponiveis = sorted(df_f3['Mes_Num'].dropna().unique().astype(int).tolist())

    nomes_meses = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho',
                       7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}

    
    if not meses_disponiveis:
        st.info("A seleção atual não possui dados mensais estruturados para filtragem de período.")
        df_final = df_f3 
    else:
        st.markdown("**Período de Análise**")
        col_mes1, col_mes2 = st.columns(2)
        
        st.markdown("**Período de Análise**")
        col_mes1, col_mes2 = st.columns(2)
        
        with col_mes1:
            mes_inicio = st.selectbox("Mês Inicial:", options=meses_disponiveis, format_func=lambda x: nomes_meses.get(x, str(x)))
        with col_mes2:
            opcoes_fim = [m for m in meses_disponiveis if m >= mes_inicio]
            mes_fim = st.selectbox("Mês Final:", options=opcoes_fim, index=len(opcoes_fim)-1, format_func=lambda x: nomes_meses.get(x, str(x)))

        df_final = df_f3[(df_f3['Mes_Num'] >= mes_inicio) & (df_f3['Mes_Num'] <= mes_fim)]

    st.markdown("---")

    if df_final.empty:
        st.info("Nenhum registro encontrado para a combinação de filtros selecionada.")
        return

    df_final['Mês_nome'] = df_final['Mes_Num'].map(nomes_meses).fillna("Não Informado")

    # 5. Métricas e Gráfico de Linhas (com segmentação por Ação)
    col_metric, col_tabela = st.columns([1, 2])

    with col_metric:
        col_metric.subheader("Indicadores da Seleção Atual")
        st.metric("Escolas Contempladas (Seleção Atual)", df_final["Nome da Escola"].nunique()) 
        st.metric("Total de Ações Registradas (Seleção Atual)", int(df_final["Valor"].sum()))
        
        # Consolidação da pizza que mostra a distribuição macro entre as ações selecionadas
        df_totais_pizza = df_final.groupby("Ação")["Valor"].sum().reset_index()
        fig_pizza = px.pie(df_totais_pizza, names="Ação", values="Valor", title="Proporção entre as Ações (Total)")
        
        # Correção de layout: Legenda embaixo e respiro de altura
        fig_pizza.update_layout(
            height=500, # Garante espaço vertical para renderizar o círculo e a legenda
            margin=dict(l=0, r=0, t=40, b=0),
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.1,
                xanchor="center",
                x=0.5
            )
        )
        # Otimiza o texto dentro da pizza para evitar sujeira visual
        fig_pizza.update_traces(textposition='inside', textinfo='percent')

        # use_container_width é a sintaxe mais moderna recomendada no lugar de width='stretch'
        st.plotly_chart(fig_pizza, width='stretch')

    with col_tabela:
        df_plot = df_final.groupby(["Mes_Num", "Mês_nome", "Ação"])["Valor"].sum().reset_index()
        df_plot = df_plot.sort_values("Mes_Num")

        titulo_grafico = "Evolução Mensal Comparativa"
        fig_line = px.line(df_plot, x="Mês_nome", y="Valor", color="Ação", title=titulo_grafico, markers=True)
        fig_line.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400)
        st.plotly_chart(fig_line, width='stretch')

    # 6. Gráficos de quebra estrutural (com cor por Ação para garantir sanidade analítica)
    gera_graficos_comparativos_estruturais(df_filtrado_acoes, df_final, num_acoes)

    #st.dataframe(df_final, width='stretch', hide_index=True)
    # 7. Exibição de Tabelas
    df_exibicao = df_final[["Coordenadoria de Região", "Unidade de Saúde", "Código INEP - Nome da Escola", "Ação", "Mês_nome", "Valor"]]
    df_filtrado = df_final[df_final["Valor"] > 0][["Coordenadoria de Região", "Unidade de Saúde", "Código INEP - Nome da Escola", "Ação", "Mês_nome", "Valor"]]
    col1, col2, col3 = st.columns(3)

    if "mostrar_tabela_comparativa" not in st.session_state:
            st.session_state["mostrar_tabela_comparativa"] = False

    with col2:
        if st.button("Exibir Tabela Comparativa dos registros filtrados"):
            st.session_state["mostrar_tabela_comparativa"] = not st.session_state["mostrar_tabela_comparativa"]
   
    if st.session_state["mostrar_tabela_comparativa"]:
        st.dataframe(df_filtrado, width='stretch', hide_index=True)

    if "mostrar_tabela_geral" not in st.session_state:
        st.session_state["mostrar_tabela_geral"] = False
    
    with col2:
        if st.button("Exibir Tabela Completa dos Registros destas Ações"):
            st.session_state["mostrar_tabela_geral"] = not st.session_state["mostrar_tabela_geral"]
    if st.session_state["mostrar_tabela_geral"]:
        st.dataframe(df_exibicao, width='stretch', hide_index=True)
    
def gera_graficos_comparativos_estruturais(df_dados, df_final, num_acoes):
    st.markdown("---")
    st.markdown("**Comparativo por Região, US e Escola (após aplicação dos filtros)**", text_alignment="center")
    
    # 1. Região (usando df_dados bruto para mostrar o macro, segmentado por ação)
    df_regiao = df_dados.groupby(["Coordenadoria de Região", "Ação"])["Valor"].sum().reset_index()
    fig_bar_regiao = px.bar(
        df_regiao, 
        y="Coordenadoria de Região", 
        x="Valor", 
        color="Ação", 
        title="Volume Comparativo por Coordenadoria de Região", 
        orientation='h',
        text_auto=True
        
    )
    fig_bar_regiao.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400, barmode='relative')
    st.plotly_chart(fig_bar_regiao, width='stretch')

    # 2. US (Filtrado)
    # Primeiro acha as Top N Unidades pelo total somado, para não quebrar a ordenação
    top_us = df_final.groupby("Unidade de Saúde")["Valor"].sum().nlargest(num_acoes).index
    df_us = df_final[df_final["Unidade de Saúde"].isin(top_us)].groupby(["Unidade de Saúde", "Ação"])["Valor"].sum().reset_index()
    
    fig_bar_us = px.bar(
        df_us, 
        y="Unidade de Saúde", 
        x="Valor", 
        color="Ação", 
        title=f"Comparativo por Unidade de Saúde (Top {num_acoes})", 
        orientation='h',
        text_auto=True
    )
    # Ordenação baseada no agrupamento total da US
    fig_bar_us.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400, barmode='stack', yaxis={'categoryorder':'total ascending'})
    st.plotly_chart(fig_bar_us, width='stretch')

    # 3. Escola (Filtrado)
    top_escolas = df_final.groupby("Código INEP - Nome da Escola")["Valor"].sum().nlargest(num_acoes).index
    df_escola = df_final[df_final["Código INEP - Nome da Escola"].isin(top_escolas)].groupby(["Código INEP - Nome da Escola", "Ação"])["Valor"].sum().reset_index()
    
    fig_bar_escola = px.bar(
        df_escola, 
        y="Código INEP - Nome da Escola", 
        x="Valor", 
        color="Ação", 
        title=f"Comparativo por Escola (Top {num_acoes})", 
        orientation='h',
        text_auto=True
    )
    fig_bar_escola.update_layout(margin=dict(l=0, r=0, t=40, b=0), height=400, barmode='stack', yaxis={'categoryorder':'total ascending'})
    st.plotly_chart(fig_bar_escola, width='stretch')
    
def plot_pizza(pizza_df, titulo):
    labels = pizza_df["descricao"].tolist()
    valores = pizza_df["valor"].tolist()
    total = sum(valores)

    if total <= 0:
        st.info("Sem valores para gerar o gráfico de pizza.")
        return

    cores = plt.colormaps.get_cmap("tab20").colors[:len(valores)]

    fig, ax = plt.subplots(figsize=(10, 8))
    wedges, texts, autotexts = ax.pie(
        valores,
        autopct=lambda v: f"{sum(valores) * v / 100:.0f}" if v > 0 else "",
        startangle=90,
        colors=cores,
    )
    ax.set_title(titulo, fontsize=24, pad=16, loc='center')
    ax.axis("equal")
    ax.legend(wedges, labels, title="Ações", loc="center left", bbox_to_anchor=(1, 0.5))
    st.pyplot(fig)
def apresenta_aba_sql(aba_selecionada):
    st.subheader(f"Ação: {aba_selecionada}")

    # Executa a busca baseada estritamente na ação selecionada
    query_dados = """
        SELECT Us_gerencia as "Coordenadoria de Região", Us as "Unidade de Saúde", Inep as "Nome da Escola", Mês as "Mês", Valor
        FROM registros_pse
        WHERE Acao_PSE = ?
    """
    df_dados = executar_query(query_dados, (aba_selecionada,))

    if df_dados.empty:
        st.warning(f"Não foram encontrados dados da ação {aba_selecionada}")
        return

    # Contagem única de escolas atingidas por esta atividade específica
    # Conta combinações únicas de Nome da Unidade e Inep, evitando sumiço de escolas sem código
    total_escolas_acao = df_dados.groupby(["Unidade de Saúde", "Nome da Escola"], dropna=False).ngroups    
    st.write(f"**Quantidade de escolas contempladas por esta ação:** {total_escolas_acao}")

    # Tratamento para abas que não possuem dados mensais estruturados (ex: Dengue)
    if df_dados["Mês"].isna().all():
        df_exibir = df_dados.drop(columns=["Mês"]).rename(columns={"Valor": "Total Registrado"})
    else:
        # Preenche temporariamente nulos caso haja mistura de dados mensais e globais
        df_dados["Mês"] = df_dados["Mês"].fillna("Não Informado")
        df_pivot = df_dados.pivot_table(
            index=["Coordenadoria de Região", "Unidade de Saúde", "Nome da Escola"],
            columns="Mês",
            values="Valor",
            aggfunc="sum"
        ).reset_index()

        meses = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", 
                     "julho", 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']

        # Transforma os números dos meses em strings de cabeçalho limpas
        df_pivot.columns = [
            #renomeia mes 1 com janeiro mes 2 com fevereiro etc
            f"{meses[int(float(col)) - 1]}" if str(col).replace('.0', '').isdigit() and 1 <= int(float(col)) <= 12 else str(col)
            for col in df_pivot.columns
        ]
        df_exibir = df_pivot

    st.dataframe(df_exibir, width='stretch', hide_index=True)

def mostra_botoes_aba(acoes_disponiveis):
    if not acoes_disponiveis:
        st.info("Nenhuma ação registrada no banco de dados.")
        return

    st.write("Selecione uma ação para visualizar o detalhamento:")
    
    if "aba_selecionada" not in st.session_state:
        st.session_state["aba_selecionada"] = None

    cols = st.columns(4)
    for i, nome_aba in enumerate(acoes_disponiveis):
        col = cols[i % 4]
        with col:
            if st.button(nome_aba, key=f"btn_aba_{i}"):
                if st.session_state["aba_selecionada"] == nome_aba:
                    st.session_state["aba_selecionada"] = None
                else:
                    st.session_state["aba_selecionada"] = nome_aba

    aba_selecionada = st.session_state["aba_selecionada"]
    if aba_selecionada is not None:
        apresenta_aba_sql(aba_selecionada)

def main():
    st.set_page_config(page_title="Programa Saúde na Escola", layout="wide")
    st.title("DASHBOARD PSE", text_alignment="center")

    # --- ÁREA DE ADMINISTRAÇÃO / UPLOAD ---
    with st.sidebar.expander("⚙️ Administração de Dados", expanded=False):
        arquivo_xlsx = st.file_uploader("Subir nova planilha PSE (.xlsx)", type=["xlsx"])
        
        if arquivo_xlsx and st.button("Processar e Atualizar Banco"):
            with st.spinner("Executando ETL. Isso pode levar alguns segundos..."):
                try:
                    sucesso, linhas = processar_upload_excel(arquivo_xlsx)
                    if sucesso:
                        st.success(f"Ingestão concluída! {linhas} registros inseridos.")
                        # CRÍTICO: Limpar o cache para forçar a leitura do novo banco
                        st.cache_data.clear() 
                        st.rerun() # Recarrega a página com os dados novos
                    else:
                        st.warning("Nenhuma aba granular válida foi processada.")
                except Exception as e:
                    st.error(f"Erro durante o processamento: {e}")

    # --- TRAVA DE SEGURANÇA ---
    # Interrompe a execução do dashboard se o banco não existir
    if not os.path.exists("dados_pse.db"):
        st.warning("⚠️ Banco de dados não encontrado. Por favor, faça o upload da planilha Excel no menu lateral.")
        st.stop()

    # --- LÓGICA EXISTENTE DO DASHBOARD ---
    nomes_renomeados = {"Visão Geral", "Múltiplas Ações", "Antropometria", "Alimentação saudável", 
                        "Práticas corporais (atividades físicas)", "Saúde mental",
                        "Prevenção de violências", "Saúde bucal", "Saúde auditiva",
                        "Saúde ocular", "Educação ambiental", "Combate à dengue",
                        "Cultura de paz e direitos humanos", "Saúde sexual e reprodutiva",
                        "Situação vacinal", "Agravos negligenciados",
                        "Dependência química (álcool e outras drogas)",
                        "Semana Saúde na Escola"}

    try:
        indicadores = executar_query("SELECT * FROM indicadores_globais")
        acoes_df = executar_query("SELECT DISTINCT Acao_PSE FROM registros_pse")
        acoes_disponiveis = acoes_df["Acao_PSE"].tolist()
        df_dados = executar_query("""SELECT Us_gerencia as 'Coordenadoria de Região', Inep as 'Nome da Escola',
                       Us as 'Unidade de Saúde', Acao_PSE as 'Ação', Mês as 'Mês', Valor FROM registros_pse""")
        
        for nome in nomes_renomeados:
            if nome not in acoes_disponiveis:
                acoes_disponiveis.append(nome)

        pizza_atividades_df = executar_query("""
            SELECT Acao_PSE as Ação, SUM(Valor) as valor 
            FROM registros_pse 
            GROUP BY Acao_PSE
        """)
        
        pizza_escolas_df = executar_query("""
            SELECT Acao_PSE as Ação, COUNT(DISTINCT "Inep") as valor 
            FROM registros_pse 
            GROUP BY Acao_PSE
        """)

    except Exception as e:
        st.error(f"Falha crítica na leitura do banco de dados: {e}")
        return

    st.sidebar.title("Navegação PSE")
    # Reordena para manter as opções de controle no topo, separadas das ações individuais
    opcoes_controle = ["Visão Geral", "Múltiplas Ações"]
    acoes_puras = sorted([a for a in acoes_disponiveis if a not in opcoes_controle])
    menu_opcoes = opcoes_controle + acoes_puras

    selecao = st.sidebar.selectbox("Filtre pela ação desejada, visão geral ou múltiplas ações👇:", menu_opcoes)

    if selecao == "Visão Geral":
        num_acoes_prioritarias = st.sidebar.selectbox("Selecione o número de ações prioritárias mínimas para visualizar as escolas que as realizaram e quais ações estão faltando.👇", [4,3,2,1])
        num_acoes = st.sidebar.selectbox("Selecione o número de ações para exibir nos gráficos de barras.👇:", [5, 10, 15])
        renderizar_dashboard_geral(indicadores, df_dados, pizza_atividades_df, pizza_escolas_df, num_acoes, num_acoes_prioritarias)
    
    elif selecao == "Múltiplas Ações":
        # Passa apenas as ações reais para o multiselect
        num_acoes = st.sidebar.selectbox("Selecione o número de US/Escolas para exibir.👇:", [5, 10, 15, 20])
        renderizar_dashboard_multiplas_acoes(df_dados, acoes_puras, num_acoes)
    
    else:
        num_acoes = st.sidebar.selectbox("Selecione o número de US/Escolas para exibir.👇:", [5, 10, 15, 20])
        renderizar_dashboard_acao(selecao, num_acoes)

    st.write("Fonte dos dados: Banco de Dados Relacional (dados_pse.db)")


if __name__ == "__main__":
    main()