import React, { useState } from 'react';
import './App.css';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui/tabs';
import { Badge } from './components/ui/badge';
import { Button } from './components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from './components/ui/collapsible';
import { ChevronDown, ChevronUp, Clock, User, Heart, AlertTriangle } from 'lucide-react';
import dietaData from './data/dietaData.json';

function App() {
  const [openRecipes, setOpenRecipes] = useState({});
  const [openSubstitutions, setOpenSubstitutions] = useState({});

  const toggleRecipe = (mealType, recipeId) => {
    const key = `${mealType}-${recipeId}`;
    setOpenRecipes(prev => ({
      ...prev,
      [key]: !prev[key]
    }));
  };

  const toggleSubstitution = (mealType, recipeId) => {
    const key = `${mealType}-${recipeId}`;
    setOpenSubstitutions(prev => ({
      ...prev,
      [key]: !prev[key]
    }));
  };

  const renderIngredients = (ingredientes) => {
    return (
      <ul className="list-disc list-inside space-y-1 text-sm text-muted-foreground">
        {ingredientes.map((ingrediente, index) => (
          <li key={index}>{ingrediente}</li>
        ))}
      </ul>
    );
  };

  const renderPreparo = (preparo) => {
    if (Array.isArray(preparo)) {
      return (
        <ol className="list-decimal list-inside space-y-2 text-sm">
          {preparo.map((passo, index) => (
            <li key={index}>{passo}</li>
          ))}
        </ol>
      );
    }
    return <p className="text-sm">{preparo}</p>;
  };

  const renderSubstitutions = (substituicoes) => {
    if (!substituicoes) return null;
    
    return (
      <div className="space-y-2">
        {Object.entries(substituicoes).map(([categoria, opcoes]) => (
          <div key={categoria} className="text-sm">
            <span className="font-medium capitalize">{categoria.replace('_', ' ')}: </span>
            <span className="text-muted-foreground">
              {Array.isArray(opcoes) ? opcoes.join(', ') : opcoes}
            </span>
          </div>
        ))}
      </div>
    );
  };

  const renderMealOptions = (refeicao, mealType) => {
    return (
      <div className="space-y-4">
        <div className="mb-4">
          <div className="flex items-center gap-2 mb-2">
            <Clock className="h-4 w-4 text-primary" />
            <span className="text-sm font-medium">{refeicao.horario}</span>
          </div>
          <p className="text-sm text-muted-foreground">{refeicao.foco}</p>
        </div>
        
        {refeicao.opcoes.map((opcao) => {
          const recipeKey = `${mealType}-${opcao.id}`;
          const isRecipeOpen = openRecipes[recipeKey];
          const isSubstitutionOpen = openSubstitutions[recipeKey];
          
          return (
            <Card key={opcao.id} className="border-l-4 border-l-primary">
              <CardHeader className="pb-3">
                <CardTitle className="text-lg">{opcao.nome}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div>
                  <h4 className="font-medium mb-2">Ingredientes:</h4>
                  {renderIngredients(opcao.ingredientes)}
                </div>
                
                {opcao.preparo && (
                  <Collapsible open={isRecipeOpen} onOpenChange={() => toggleRecipe(mealType, opcao.id)}>
                    <CollapsibleTrigger asChild>
                      <Button variant="outline" className="w-full justify-between">
                        Ver modo de preparo
                        {isRecipeOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="mt-3">
                      <div className="p-4 bg-muted rounded-lg">
                        <h4 className="font-medium mb-2">Modo de preparo:</h4>
                        {renderPreparo(opcao.preparo)}
                      </div>
                    </CollapsibleContent>
                  </Collapsible>
                )}
                
                {opcao.substituicoes && (
                  <Collapsible open={isSubstitutionOpen} onOpenChange={() => toggleSubstitution(mealType, opcao.id)}>
                    <CollapsibleTrigger asChild>
                      <Button variant="secondary" className="w-full justify-between">
                        Ver substituições
                        {isSubstitutionOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="mt-3">
                      <div className="p-4 bg-secondary/50 rounded-lg">
                        <h4 className="font-medium mb-2">Substituições equivalentes:</h4>
                        {renderSubstitutions(opcao.substituicoes)}
                      </div>
                    </CollapsibleContent>
                  </Collapsible>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-green-50 to-blue-50">
      <div className="container mx-auto px-4 py-8">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold text-primary mb-2">
            Dieta Gestacional Personalizada
          </h1>
          <p className="text-lg text-muted-foreground">
            Acompanhamento nutricional para diabetes gestacional
          </p>
        </div>

        {/* Patient Info */}
        <Card className="mb-8">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="h-5 w-5" />
              Informações da Paciente
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              <div>
                <span className="font-medium">Idade:</span> {dietaData.paciente.idade} anos
              </div>
              <div>
                <span className="font-medium">Peso:</span> {dietaData.paciente.peso} kg
              </div>
              <div>
                <span className="font-medium">Gestação:</span> {dietaData.paciente.gestacao}
              </div>
              <div>
                <span className="font-medium">Diagnóstico:</span> {dietaData.paciente.diagnostico}
              </div>
              <div>
                <span className="font-medium">Biotipo:</span> {dietaData.paciente.biotipo}
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Main Content */}
        <Tabs defaultValue="refeicoes" className="space-y-6">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="refeicoes" className="flex items-center gap-2">
              <Heart className="h-4 w-4" />
              Refeições
            </TabsTrigger>
            <TabsTrigger value="enjoos" className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4" />
              Alívio de Enjoos
            </TabsTrigger>
          </TabsList>

          <TabsContent value="refeicoes">
            <Tabs defaultValue="cafe_manha" orientation="vertical" className="flex gap-6">
              <TabsList className="flex flex-col h-fit w-64 space-y-1">
                <TabsTrigger value="cafe_manha" className="w-full justify-start">
                  Café da Manhã
                </TabsTrigger>
                <TabsTrigger value="lanche_manha" className="w-full justify-start">
                  Lanche da Manhã
                </TabsTrigger>
                <TabsTrigger value="almoco" className="w-full justify-start">
                  Almoço
                </TabsTrigger>
                <TabsTrigger value="lanche_tarde" className="w-full justify-start">
                  Lanche da Tarde
                </TabsTrigger>
                <TabsTrigger value="jantar" className="w-full justify-start">
                  Jantar
                </TabsTrigger>
                <TabsTrigger value="lanche_noite" className="w-full justify-start">
                  Lanche da Noite
                </TabsTrigger>
              </TabsList>

              <div className="flex-1">
                {Object.entries(dietaData.refeicoes).map(([key, refeicao]) => (
                  <TabsContent key={key} value={key} className="mt-0">
                    <Card>
                      <CardHeader>
                        <CardTitle>{refeicao.titulo}</CardTitle>
                        <CardDescription>{refeicao.foco}</CardDescription>
                      </CardHeader>
                      <CardContent>
                        {renderMealOptions(refeicao, key)}
                      </CardContent>
                    </Card>
                  </TabsContent>
                ))}
              </div>
            </Tabs>
          </TabsContent>

          <TabsContent value="enjoos">
            <div className="space-y-6">
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <AlertTriangle className="h-5 w-5" />
                    {dietaData.enjoos.titulo}
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-6">
                  <div>
                    <h3 className="font-semibold mb-3">Orientações Gerais:</h3>
                    <ul className="list-disc list-inside space-y-1 text-sm">
                      {dietaData.enjoos.orientacoes.map((orientacao, index) => (
                        <li key={index}>{orientacao}</li>
                      ))}
                    </ul>
                  </div>

                  <div>
                    <h3 className="font-semibold mb-3">Alimentos e Bebidas que Auxiliam:</h3>
                    <div className="grid gap-3">
                      {dietaData.enjoos.alimentos_bebidas.map((item, index) => (
                        <div key={index} className="p-3 bg-muted rounded-lg">
                          <h4 className="font-medium">{item.nome}</h4>
                          <p className="text-sm text-muted-foreground">{item.descricao}</p>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <h3 className="font-semibold mb-3">Refeições para Dias de Muito Enjoo:</h3>
                    <div className="space-y-4">
                      {dietaData.enjoos.refeicoes_enjoo.map((refeicao, index) => (
                        <Card key={index} className="border-l-4 border-l-orange-500">
                          <CardHeader className="pb-3">
                            <CardTitle className="text-lg">{refeicao.nome}</CardTitle>
                          </CardHeader>
                          <CardContent>
                            <div className="space-y-3">
                              <div>
                                <h4 className="font-medium mb-2">Ingredientes:</h4>
                                <div className="flex flex-wrap gap-1">
                                  {refeicao.ingredientes.map((ingrediente, idx) => (
                                    <Badge key={idx} variant="secondary" className="text-xs">
                                      {ingrediente}
                                    </Badge>
                                  ))}
                                </div>
                              </div>
                              <div>
                                <h4 className="font-medium mb-2">Preparo:</h4>
                                <p className="text-sm text-muted-foreground">{refeicao.preparo}</p>
                              </div>
                            </div>
                          </CardContent>
                        </Card>
                      ))}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

export default App;

